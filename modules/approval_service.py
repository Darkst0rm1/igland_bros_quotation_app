"""Internal approval: when it is required, who may give it, and what it gates.

Two rules are enforced by identity rather than by permission, and no grant can
route around either:

* **Nobody approves their own quotation** — not a Sales Manager, not a System
  Administrator. The check compares user ids and runs before any permission is
  consulted.
* **A document cannot be released while approval is outstanding.** Only a
  DRAFT-marked copy is available until a decision is recorded.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules import pricing_service, settings_service
from modules.audit_service import record_audit
from modules.authorization import AuthUser, ApprovalLimits, require
from modules.calculation_engine import ZERO
from modules.constants import (
    ApprovalDecision,
    ApprovalTrigger,
    AuditAction,
    EntityType,
    Perm,
    QuotationStatus,
)
from modules.models import Approval, Customer, Quotation
from modules.quotation_service import QuotationError, change_status

log = logging.getLogger(__name__)


class ApprovalError(ValueError):
    """An approval operation that failed a business rule. Safe to show the user."""


@dataclass(frozen=True)
class TriggeredRule:
    trigger: ApprovalTrigger
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"trigger": self.trigger.value, "message": self.message}


# --------------------------------------------------------------------------- #
# Rule engine
# --------------------------------------------------------------------------- #

def evaluate(
    session: Session, quotation: Quotation, user: AuthUser
) -> list[TriggeredRule]:
    """Which approval rules a quotation trips, for the user raising it.

    Limits are the *submitter's*, not the approver's: the question is whether
    this person may issue this quotation on their own authority.
    """
    limits: ApprovalLimits = user.limits
    triggered: list[TriggeredRule] = []

    # --- custom pricing ------------------------------------------------- #
    custom_lines = [i for i in quotation.items if i.is_custom_price]
    if custom_lines:
        lines = ", ".join(str(i.line_no) for i in custom_lines)
        triggered.append(
            TriggeredRule(
                ApprovalTrigger.CUSTOM_PRICE_USED,
                f"Custom pricing is used on line(s) {lines}.",
            )
        )

    # --- discount above the submitter's limit --------------------------- #
    worst_line_discount = max(
        (i.line_discount_pct or ZERO for i in quotation.items), default=ZERO
    )
    effective_discount = max(worst_line_discount, quotation.quote_discount_pct or ZERO)
    if limits.discount_exceeds(effective_discount):
        triggered.append(
            TriggeredRule(
                ApprovalTrigger.DISCOUNT_ABOVE_LIMIT,
                f"A discount of {effective_discount:g}% exceeds your limit of "
                f"{limits.max_discount_pct:g}%.",
            )
        )

    # --- value above the submitter's authority -------------------------- #
    if limits.value_exceeds(quotation.grand_total or ZERO):
        triggered.append(
            TriggeredRule(
                ApprovalTrigger.VALUE_ABOVE_AUTHORITY,
                f"The total of {quotation.grand_total:,.2f} {quotation.currency} "
                f"exceeds your authority of {limits.max_quote_value:,.2f}.",
            )
        )

    # --- margin below threshold ----------------------------------------- #
    # Only meaningful once costs exist; with none recorded the margin is None
    # and this rule stays silent rather than firing on a phantom zero.
    if limits.margin_below(quotation.gross_margin_pct):
        triggered.append(
            TriggeredRule(
                ApprovalTrigger.MARGIN_BELOW_THRESHOLD,
                f"The gross margin of {quotation.gross_margin_pct:g}% is below the "
                f"minimum of {limits.min_margin_pct:g}%.",
            )
        )

    # --- payment terms beyond what the customer has agreed -------------- #
    customer = session.get(Customer, quotation.customer_id)
    if customer and customer.payment_terms_days:
        quoted_days = _quoted_payment_days(quotation)
        if quoted_days is not None and quoted_days > customer.payment_terms_days:
            triggered.append(
                TriggeredRule(
                    ApprovalTrigger.PAYMENT_TERMS_EXCEEDED,
                    f"Payment terms of {quoted_days} days exceed the "
                    f"{customer.payment_terms_days} agreed with this customer.",
                )
            )

    # --- expired price used --------------------------------------------- #
    from modules.constants import PriceWarningCode

    warnings = pricing_service.evaluate_quotation(session, quotation)
    if any(w.code is PriceWarningCode.PRICE_EXPIRED for w in warnings):
        triggered.append(
            TriggeredRule(
                ApprovalTrigger.EXPIRED_PRICE_USED,
                "An expired price is used on this quotation.",
            )
        )
    if any(w.code is PriceWarningCode.CUSTOM_PRICE_BELOW_FLOOR for w in warnings):
        triggered.append(
            TriggeredRule(
                ApprovalTrigger.PRICE_MANUALLY_OVERRIDDEN,
                "A custom price is below the permitted floor.",
            )
        )

    return triggered


def _quoted_payment_days(quotation: Quotation) -> int | None:
    """Read a day count out of the quotation's payment terms, if it states one.

    Deliberately conservative: only an explicit "NN days" counts. Free text
    like "payment upon receipt" returns None rather than a guess, because a
    wrong guess here would raise a spurious approval on every quotation.
    """
    import re

    from modules.constants import TermSection

    for term in quotation.terms:
        if term.section is not TermSection.PAYMENT_TERMS:
            continue
        match = re.search(r"(\d{1,3})\s*days?", term.body_text or "", re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def requires_approval(
    session: Session, quotation: Quotation, user: AuthUser
) -> bool:
    return bool(evaluate(session, quotation, user))


# --------------------------------------------------------------------------- #
# Submission
# --------------------------------------------------------------------------- #

def submit(
    session: Session, quotation: Quotation, user: AuthUser, note: str | None = None
) -> Approval | None:
    """Submit for approval, or approve outright when no rule fires.

    Returns the ``Approval`` when one was raised, or ``None`` when the
    quotation went straight to Approved because the submitter had authority for
    everything on it.
    """
    require(user, Perm.QUOTE_SUBMIT_FOR_APPROVAL)

    if quotation.status is not QuotationStatus.DRAFT:
        raise ApprovalError(
            f"Only a draft can be submitted; this one is {quotation.status.value}."
        )

    from modules.quotation_service import validate_for_submission

    problems = validate_for_submission(session, quotation)
    if problems:
        raise ApprovalError("The quotation is not complete: " + "; ".join(problems))

    triggered = evaluate(session, quotation, user)

    if not triggered:
        quotation.requires_approval = False
        change_status(session, user, quotation, QuotationStatus.APPROVED, note)
        record_audit(
            session, user, AuditAction.APPROVED, EntityType.QUOTATION, quotation.id,
            reason="no approval rules triggered; within the submitter's authority",
        )
        log.info("%s approved on submission", quotation.quote_number)
        return None

    approval = Approval(
        quotation_id=quotation.id,
        requested_by_id=user.id,
        requested_at=dt.datetime.now(dt.UTC),
        decision=ApprovalDecision.PENDING,
        comments=note,
        triggered_rules_json=[rule.as_dict() for rule in triggered],
    )
    session.add(approval)
    quotation.requires_approval = True
    change_status(session, user, quotation, QuotationStatus.PENDING_APPROVAL, note)
    session.flush()

    record_audit(
        session, user, AuditAction.APPROVAL_REQUESTED, EntityType.APPROVAL, approval.id,
        new_value={
            "quotation": quotation.quote_number,
            "triggers": [r.trigger.value for r in triggered],
        },
        reason=note,
    )
    log.info(
        "%s submitted for approval (%d rule(s))",
        quotation.quote_number, len(triggered),
    )
    return approval


def pending_approval(session: Session, approval_id: int) -> Approval:
    approval = session.get(Approval, approval_id)
    if approval is None:
        raise ApprovalError("That approval request no longer exists.")
    if approval.decision is not ApprovalDecision.PENDING:
        raise ApprovalError(
            f"This request was already {approval.decision.value.lower()}."
        )
    return approval


def open_approval_for(session: Session, quotation_id: int) -> Approval | None:
    return session.execute(
        select(Approval)
        .where(
            Approval.quotation_id == quotation_id,
            Approval.decision == ApprovalDecision.PENDING,
        )
        .order_by(Approval.id.desc())
    ).scalars().first()


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #

def _assert_may_decide(approval: Approval, quotation: Quotation, user: AuthUser) -> None:
    """Self-approval is impossible, for everyone.

    The identity comparison happens first and is not reachable by any
    permission grant — a System Administrator holds ``quote.approve`` and still
    cannot approve a quotation they raised or own.
    """
    if user.id == approval.requested_by_id:
        raise ApprovalError(
            "You submitted this quotation, so you cannot also approve it. "
            "Another manager must decide."
        )
    if user.id == quotation.sales_user_id:
        raise ApprovalError(
            "This is your own quotation, so you cannot approve it. "
            "Another manager must decide."
        )
    require(user, Perm.QUOTE_APPROVE)


def approve(
    session: Session,
    quotation: Quotation,
    user: AuthUser,
    approval_id: int,
    comments: str | None = None,
    override_reason: str | None = None,
) -> Approval:
    approval = pending_approval(session, approval_id)
    _assert_may_decide(approval, quotation, user)

    # Blocking pricing problems must be resolved or explicitly overridden; a
    # manager cannot approve past a missing price by not looking at it.
    warnings = pricing_service.evaluate_quotation(session, quotation)
    blocking = pricing_service.blocking(warnings)
    if blocking:
        unresolvable = [w for w in blocking if not w.overridable]
        if unresolvable:
            raise ApprovalError(
                "This quotation cannot be approved until these are fixed: "
                + "; ".join(w.message for w in unresolvable)
            )
        if not override_reason or not override_reason.strip():
            raise ApprovalError(
                "This quotation has blocking warnings. Supply an override reason to "
                "approve it anyway: " + "; ".join(w.message for w in blocking)
            )
        require(user, Perm.QUOTE_OVERRIDE_WARNING)
        record_audit(
            session, user, AuditAction.WARNING_OVERRIDDEN, EntityType.QUOTATION,
            quotation.id,
            new_value={"overridden": [w.code.value for w in blocking]},
            reason=override_reason,
        )

    approval.decision = ApprovalDecision.APPROVED
    approval.approver_id = user.id
    approval.decided_at = dt.datetime.now(dt.UTC)
    approval.comments = comments
    approval.override_reason = override_reason
    session.flush()

    change_status(session, user, quotation, QuotationStatus.APPROVED, comments)
    record_audit(
        session, user, AuditAction.APPROVED, EntityType.APPROVAL, approval.id,
        new_value={"quotation": quotation.quote_number}, reason=comments,
    )
    log.info("%s approved by %s", quotation.quote_number, user.username)
    return approval


def reject(
    session: Session,
    quotation: Quotation,
    user: AuthUser,
    approval_id: int,
    reason: str,
) -> Approval:
    if not reason or not reason.strip():
        raise ApprovalError("A reason is required to reject a quotation.")

    approval = pending_approval(session, approval_id)
    _assert_may_decide(approval, quotation, user)
    require(user, Perm.QUOTE_REJECT)

    approval.decision = ApprovalDecision.REJECTED
    approval.approver_id = user.id
    approval.decided_at = dt.datetime.now(dt.UTC)
    approval.rejection_reason = reason
    session.flush()

    change_status(
        session, user, quotation, QuotationStatus.REJECTED_INTERNALLY, reason
    )
    record_audit(
        session, user, AuditAction.REJECTED, EntityType.APPROVAL, approval.id,
        new_value={"quotation": quotation.quote_number}, reason=reason,
    )
    return approval


def return_for_revision(
    session: Session,
    quotation: Quotation,
    user: AuthUser,
    approval_id: int,
    reason: str,
) -> Approval:
    if not reason or not reason.strip():
        raise ApprovalError("A reason is required to return a quotation for revision.")

    approval = pending_approval(session, approval_id)
    _assert_may_decide(approval, quotation, user)
    require(user, Perm.QUOTE_RETURN_FOR_REVISION)

    approval.decision = ApprovalDecision.RETURNED_FOR_REVISION
    approval.approver_id = user.id
    approval.decided_at = dt.datetime.now(dt.UTC)
    approval.rejection_reason = reason
    session.flush()

    change_status(
        session, user, quotation, QuotationStatus.REVISION_REQUIRED, reason
    )
    record_audit(
        session, user, AuditAction.REJECTED, EntityType.APPROVAL, approval.id,
        new_value={"quotation": quotation.quote_number, "returned": True},
        reason=reason,
    )
    return approval


# --------------------------------------------------------------------------- #
# The release gate
# --------------------------------------------------------------------------- #

def release_blockers(session: Session, quotation: Quotation) -> list[str]:
    """Why a final document cannot be produced yet. Empty means it can.

    Checked by ``document_service`` for **both** formats — there is no route by
    which a Word file escapes a gate the PDF is subject to.
    """
    reasons: list[str] = []

    if quotation.status in {
        QuotationStatus.DRAFT,
        QuotationStatus.PENDING_APPROVAL,
        QuotationStatus.REJECTED_INTERNALLY,
        QuotationStatus.REVISION_REQUIRED,
        QuotationStatus.CANCELLED,
    }:
        reasons.append(
            f"The quotation is {quotation.status.value.replace('_', ' ').lower()}; "
            "it must be approved before a final document can be issued."
        )

    if open_approval_for(session, quotation.id) is not None:
        reasons.append("An approval request is still awaiting a decision.")

    blocking = pricing_service.blocking(
        pricing_service.evaluate_quotation(session, quotation)
    )
    if blocking and quotation.status is not QuotationStatus.APPROVED:
        reasons += [w.message for w in blocking]

    from modules.quotation_service import validate_for_submission

    reasons += validate_for_submission(session, quotation)
    return reasons


def assert_release_allowed(session: Session, quotation: Quotation) -> None:
    blockers = release_blockers(session, quotation)
    if blockers:
        raise ApprovalError(
            "A final document cannot be produced yet:\n- " + "\n- ".join(blockers)
        )


def queue(session: Session, user: AuthUser) -> list[tuple[Approval, Quotation]]:
    """Pending approvals this user could actually decide.

    Their own submissions and their own quotations are filtered out here as
    well as refused at decision time, so the queue never shows work the viewer
    cannot action.
    """
    require(user, Perm.QUOTE_APPROVE)

    rows = session.execute(
        select(Approval, Quotation)
        .join(Quotation, Approval.quotation_id == Quotation.id)
        .where(
            Approval.decision == ApprovalDecision.PENDING,
            # A deleted quotation leaves its pending request behind. Without
            # this the queue offers an approver a quotation that no longer
            # appears anywhere else, and approving it would issue one.
            Quotation.deleted_at.is_(None),
        )
        .order_by(Approval.requested_at)
    ).all()

    return [
        (approval, quotation)
        for approval, quotation in rows
        if approval.requested_by_id != user.id and quotation.sales_user_id != user.id
    ]
