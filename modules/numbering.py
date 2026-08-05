"""Quotation number allocation.

The format is a company setting, defaulting to ``QT-{YYYY}-{SEQ:04d}``.

Sequence values come from the ``document_sequences`` table under a row lock,
not from ``MAX(quote_number)``. Two employees clicking "New quotation" in the
same second would otherwise both read the same maximum and both be handed
``QT-2026-0042``, and the unique constraint would reject the second one
after they had already filled the form in.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.models import DocumentSequence

log = logging.getLogger(__name__)

DEFAULT_FORMAT = "QT-{YYYY}-{SEQ:04d}"

#: Supported placeholders. {SEQ} may carry a width, e.g. {SEQ:05d}.
_PLACEHOLDER = re.compile(r"\{(YYYY|YY|MM|SEQ)(?::0?(\d+)d)?\}")


class NumberFormatError(ValueError):
    """The configured quotation-number format is unusable."""


def validate_format(fmt: str) -> None:
    """Reject a format that would not produce unique, sortable numbers.

    Called when the setting is saved rather than when a quotation is created,
    so a bad format is caught by the administrator who typed it and not by the
    salesperson who happens to click New next.
    """
    if not fmt or not fmt.strip():
        raise NumberFormatError("The quotation number format cannot be empty.")

    found = {m.group(1) for m in _PLACEHOLDER.finditer(fmt)}
    if "SEQ" not in found:
        raise NumberFormatError(
            "The format must include {SEQ} (optionally padded, e.g. {SEQ:04d}), "
            "otherwise every quotation would get the same number."
        )

    leftovers = re.sub(_PLACEHOLDER, "", fmt)
    if "{" in leftovers or "}" in leftovers:
        raise NumberFormatError(
            "The format contains an unrecognised placeholder. Supported: "
            "{YYYY}, {YY}, {MM}, {SEQ} (optionally {SEQ:04d})."
        )


def scope_key(fmt: str, on_date: date) -> str:
    """The sequence bucket a number belongs to.

    A format containing {YYYY} or {YY} restarts each year; adding {MM} restarts
    each month. A format with neither runs a single continuous sequence. Getting
    this from the format itself means the counter always matches what the number
    displays — a year-stamped number cannot silently share a counter across years.
    """
    found = {m.group(1) for m in _PLACEHOLDER.finditer(fmt)}
    parts = ["QUOTE"]
    if "YYYY" in found or "YY" in found:
        parts.append(f"{on_date.year:04d}")
    if "MM" in found:
        parts.append(f"{on_date.month:02d}")
    return ":".join(parts) if len(parts) > 1 else "QUOTE:ALL"


def render(fmt: str, sequence: int, on_date: date) -> str:
    def substitute(match: re.Match[str]) -> str:
        token, width = match.group(1), match.group(2)
        if token == "YYYY":
            return f"{on_date.year:04d}"
        if token == "YY":
            return f"{on_date.year % 100:02d}"
        if token == "MM":
            return f"{on_date.month:02d}"
        return f"{sequence:0{int(width)}d}" if width else str(sequence)

    return _PLACEHOLDER.sub(substitute, fmt)


def allocate_quote_number(
    session: Session,
    fmt: str = DEFAULT_FORMAT,
    on_date: date | None = None,
) -> str:
    """Reserve and return the next quotation number.

    Must be called inside the transaction that inserts the quotation. The row
    lock is held until that transaction commits, so a concurrent caller waits
    rather than reading a stale counter.

    ``with_for_update`` is a no-op on SQLite, which serialises writers anyway;
    on PostgreSQL it is what makes this correct under real concurrency.
    """
    validate_format(fmt)
    on_date = on_date or date.today()
    key = scope_key(fmt, on_date)

    row = session.execute(
        select(DocumentSequence).where(DocumentSequence.scope_key == key).with_for_update()
    ).scalar_one_or_none()

    if row is None:
        row = DocumentSequence(scope_key=key, last_value=0)
        session.add(row)
        session.flush()

    row.last_value += 1
    session.flush()

    number = render(fmt, row.last_value, on_date)
    log.debug("Allocated quotation number %s (scope=%s)", number, key)
    return number


def peek_next_number(
    session: Session, fmt: str = DEFAULT_FORMAT, on_date: date | None = None
) -> str:
    """The number that *would* be allocated next, for display only.

    Does not reserve anything, and is not safe to persist — by the time the user
    saves, someone else may have taken it.
    """
    validate_format(fmt)
    on_date = on_date or date.today()
    row = session.execute(
        select(DocumentSequence).where(
            DocumentSequence.scope_key == scope_key(fmt, on_date)
        )
    ).scalar_one_or_none()
    return render(fmt, (row.last_value if row else 0) + 1, on_date)
