"""The order a quotation is built in, and what each step insists on.

Deliberately free of Streamlit. The step order, the validation rules and the
question "may this quotation be edited at all" are decisions about quotations,
not about widgets, and keeping them here means they can be tested without a
browser and reused by anything that later builds a quotation — an importer, a
duplicate-quotation action, an API.

The page renders what this module decides. It does not decide anything itself.

**Why the tab strip is not ``st.tabs``.** ``st.tabs`` cannot be switched from
Python: there is no key, and no way to say "now show Lines". "Save & Continue"
is precisely a request to switch tabs from Python, so the strip has to be a
widget that carries state — ``st.segmented_control`` — which the page reads
from and writes to through :func:`active_step` and :func:`set_step`.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable


@dataclass(frozen=True)
class Step:
    """One stage of building a quotation."""

    key: str
    label: str
    #: False for stages that are not part of the build sequence at all —
    #: "Customer response" records what happened *after* sending, so it has no
    #: place in a flow whose whole purpose is to reach the send.
    in_sequence: bool = True


#: The build order. "Customer response" sits outside it: it is post-send, and
#: putting it in the sequence would imply an employee walks through it while
#: preparing a quotation, which is backwards.
STEPS: tuple[Step, ...] = (
    Step("details", "Details"),
    Step("lines", "Lines"),
    Step("shipping", "Shipping"),
    Step("charges", "Charges"),
    Step("terms", "Terms"),
    Step("review", "Review & send"),
    Step("tracking", "Customer response", in_sequence=False),
)

SEQUENCE: tuple[str, ...] = tuple(s.key for s in STEPS if s.in_sequence)
STEP_KEYS: tuple[str, ...] = tuple(s.key for s in STEPS)
STEP_LABELS: dict[str, str] = {s.key: s.label for s in STEPS}

FIRST_STEP: str = SEQUENCE[0]
LAST_STEP: str = SEQUENCE[-1]


class UnknownStep(ValueError):
    """A step key that is not part of this quotation flow."""


def _require(step: str) -> str:
    if step not in STEP_KEYS:
        raise UnknownStep(f"{step!r} is not a quotation step")
    return step


def next_step(step: str) -> str | None:
    """The step after this one, or None at the end of the sequence.

    Returns None for steps outside the sequence too: there is nothing to
    continue *to* from Customer response.
    """
    _require(step)
    if step not in SEQUENCE:
        return None
    i = SEQUENCE.index(step)
    return SEQUENCE[i + 1] if i + 1 < len(SEQUENCE) else None


def previous_step(step: str) -> str | None:
    """The step before this one, or None at the start."""
    _require(step)
    if step not in SEQUENCE:
        return None
    i = SEQUENCE.index(step)
    return SEQUENCE[i - 1] if i > 0 else None


def is_last(step: str) -> bool:
    """Whether Save & Continue should give way to the send action."""
    return _require(step) == LAST_STEP


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

#: A validator takes the page's snapshot of the quotation and returns the
#: reasons this step may not be left. Empty means "go ahead".
Validator = Callable[[dict[str, Any]], list[str]]


def _details_problems(state: dict[str, Any]) -> list[str]:
    header = state.get("header") or {}
    problems: list[str] = []

    if not header.get("customer_id"):
        problems.append("Choose a customer before continuing.")

    quote_date = header.get("quote_date")
    valid_until = header.get("valid_until")
    if isinstance(quote_date, dt.date) and isinstance(valid_until, dt.date):
        if valid_until < quote_date:
            problems.append(
                "The valid-until date is before the quote date. A quotation "
                "cannot expire before it is issued."
            )
    if not header.get("currency"):
        problems.append("Choose a currency before continuing.")
    return problems


def _lines_problems(state: dict[str, Any]) -> list[str]:
    """A quotation with no lines has no price, so it cannot be reviewed.

    Partially completed rows are reported per row by the grid and surfaced
    here as a count: the grid highlights *which*, this blocks the advance.
    """
    problems: list[str] = []
    if not state.get("lines"):
        problems.append("Add at least one line before continuing.")

    incomplete = int(state.get("incomplete_line_count") or 0)
    if incomplete:
        problems.append(
            f"{incomplete} line(s) are partly filled in. Complete them or "
            "delete them — a half-entered line is never discarded silently."
        )
    return problems


def _shipping_problems(state: dict[str, Any]) -> list[str]:
    """Shipping is genuinely optional until freight is actually charged."""
    if not state.get("freight_charged"):
        return []
    if not state.get("container_count"):
        return [
            "Freight is charged to the customer but no containers are "
            "defined, so there is nothing to price the freight against."
        ]
    return []


def _no_problems(state: dict[str, Any]) -> list[str]:
    return []


#: Charges and terms are deliberately unvalidated. Neither is required to make
#: a quotation coherent, and a wizard that refuses to advance past an empty
#: optional step teaches people to distrust its warnings.
VALIDATORS: dict[str, Validator] = {
    "details": _details_problems,
    "lines": _lines_problems,
    "shipping": _shipping_problems,
    "charges": _no_problems,
    "terms": _no_problems,
    "review": _no_problems,
    "tracking": _no_problems,
}


def validate(step: str, state: dict[str, Any]) -> list[str]:
    """Why this step may not be left yet. Empty list means it may."""
    return VALIDATORS[_require(step)](state)


def may_advance(step: str, state: dict[str, Any]) -> bool:
    return not validate(step, state)


# --------------------------------------------------------------------------- #
# Session plumbing
# --------------------------------------------------------------------------- #

def step_state_key(quotation_id: int) -> str:
    """Per-quotation, so two open quotations do not share a position."""
    return f"wizard_step_{int(quotation_id)}"


def dirty_state_key(quotation_id: int) -> str:
    return f"wizard_dirty_{int(quotation_id)}"


def saving_state_key(quotation_id: int) -> str:
    """Set while a save is in flight, so the button can refuse a second click."""
    return f"wizard_saving_{int(quotation_id)}"


def resolve_step(requested: str | None) -> str:
    """The step to show, tolerating a stale or absent request.

    A key held in session state can outlive the code that defined it — a
    renamed step must land somewhere sensible rather than raising on a page
    the user did not do anything wrong to reach.
    """
    if requested in STEP_KEYS:
        return requested  # type: ignore[return-value]
    return FIRST_STEP


def editable_steps(editable: bool) -> frozenset[str]:
    """Which steps accept input.

    A locked quotation — approved, sent, accepted, superseded — still shows
    every step, because reading a quotation you may not change is the common
    case. Nothing becomes editable by navigating to it.
    """
    if not editable:
        return frozenset()
    return frozenset(STEP_KEYS)
