"""Base, all-options and accepted totals — three figures that must not be confused."""
from __future__ import annotations

from decimal import Decimal as D

import pytest

from modules import portal_service, pricing_snapshot, quotation_service
from modules.constants import ItemInclusion, PriceTierCode, QuotationStatus
from modules.pricing_snapshot import PriceScope

from tests.test_documents_and_approval import (  # noqa: F401
    _approve_and_issue,
    admin,
    manager,
    quotation,
    sales,
    variant,
)


@pytest.fixture(autouse=True)
def portal_user(session):
    return portal_service.ensure_portal_user(session)


@pytest.fixture
def mixed(session, quotation, sales, variant):
    """One included line, one optional, one recommended."""
    for label, inclusion in (
        ("Optional extra", ItemInclusion.OPTIONAL),
        ("Recommended extra", ItemInclusion.RECOMMENDED),
    ):
        line = quotation_service.add_line(
            session, sales, quotation,
            product_variant_id=variant.id,
            price_tier_code=PriceTierCode.STANDARD.value,
            quantity_packs=D("100"),
            description_override=label,
        )
        line.inclusion = inclusion
    session.flush()
    quotation_service.recompute_totals(session, quotation)
    session.flush()
    return quotation


class TestCompatibility:
    def test_an_all_included_quotation_is_unchanged(self, session, quotation):
        """The default is INCLUDED, so existing quotations keep their figures."""
        assert all(i.inclusion is ItemInclusion.INCLUDED for i in quotation.items)
        stored = quotation.grand_total
        quotation_service.recompute_totals(session, quotation)
        session.flush()
        assert quotation.grand_total == stored
        assert pricing_snapshot.base(quotation).grand_total == stored


class TestScopes:
    def test_optional_is_excluded_from_the_base_total(self, session, mixed):
        optional = next(i for i in mixed.items if i.inclusion is ItemInclusion.OPTIONAL)
        base = pricing_snapshot.base(mixed)
        assert optional.id not in [ln.item_id for ln in base.counted_lines]

    def test_recommended_is_excluded_from_the_base_total(self, session, mixed):
        """Recommended is highlighted, not silently charged for."""
        rec = next(i for i in mixed.items if i.inclusion is ItemInclusion.RECOMMENDED)
        base = pricing_snapshot.base(mixed)
        assert rec.id not in [ln.item_id for ln in base.counted_lines]

    def test_the_stored_total_is_the_base_total(self, session, mixed):
        assert mixed.grand_total == pricing_snapshot.base(mixed).grand_total

    def test_all_options_is_the_ceiling(self, session, mixed):
        base = pricing_snapshot.base(mixed)
        ceiling = pricing_snapshot.all_options(mixed)
        assert ceiling.subtotal > base.subtotal
        assert ceiling.grand_total > base.grand_total
        assert len(ceiling.counted_lines) == len(mixed.items)

    def test_selected_sits_between_base_and_all_options(self, session, mixed):
        optional = next(i for i in mixed.items if i.inclusion is ItemInclusion.OPTIONAL)
        base = pricing_snapshot.base(mixed)
        chosen = pricing_snapshot.selected(mixed, [optional.id])
        ceiling = pricing_snapshot.all_options(mixed)

        assert base.grand_total < chosen.grand_total < ceiling.grand_total
        assert chosen.subtotal - base.subtotal == optional.net_line_total

    def test_tax_is_computed_per_scope(self, session, mixed):
        mixed.tax_rate_pct = D("13")
        session.flush()
        base = pricing_snapshot.base(mixed)
        ceiling = pricing_snapshot.all_options(mixed)
        assert ceiling.tax_amount > base.tax_amount
        for snap in (base, ceiling):
            assert snap.tax_amount == (
                snap.taxable_base * D("13") / D("100")
            ).quantize(D("0.01"))

    def test_deposit_is_computed_per_scope(self, session, mixed):
        mixed.deposit_pct = D("25")
        session.flush()
        base = pricing_snapshot.base(mixed)
        ceiling = pricing_snapshot.all_options(mixed)
        assert ceiling.deposit_due > base.deposit_due
        assert base.deposit_due == (
            base.grand_total * D("25") / D("100")
        ).quantize(D("0.01"))

    def test_optional_available_reports_what_is_left(self, session, mixed):
        base = pricing_snapshot.base(mixed)
        ceiling = pricing_snapshot.all_options(mixed)
        assert base.optional_available == ceiling.subtotal - base.subtotal
        assert ceiling.optional_available == D("0.00")

    def test_a_selection_says_which_lines_not_what_they_cost(self, session, mixed):
        optional = next(i for i in mixed.items if i.inclusion is ItemInclusion.OPTIONAL)
        honest = pricing_snapshot.selected(mixed, [optional.id])
        tampered = pricing_snapshot.selected(
            mixed, [optional.id, 999999, -1, "12; DROP TABLE quotations", optional.id]
        )
        assert tampered.grand_total == honest.grand_total
        assert tampered.selected_item_ids == honest.selected_item_ids


class TestOnePricingPath:
    def test_portal_and_snapshot_agree(self, session, mixed):
        """The portal must not have arithmetic of its own."""
        optional = next(i for i in mixed.items if i.inclusion is ItemInclusion.OPTIONAL)
        via_portal = portal_service.compute_selection_totals(mixed, [optional.id])
        via_snapshot = pricing_snapshot.selected(mixed, [optional.id])

        assert via_portal.subtotal == via_snapshot.subtotal
        assert via_portal.tax_amount == via_snapshot.tax_amount
        assert via_portal.grand_total == via_snapshot.grand_total
        assert via_portal.taxable_base == via_snapshot.taxable_base

    def test_the_employee_stored_total_and_the_snapshot_agree(self, session, mixed):
        assert mixed.subtotal == pricing_snapshot.base(mixed).subtotal
        assert mixed.tax_amount == pricing_snapshot.base(mixed).tax_amount
        assert mixed.grand_total == pricing_snapshot.base(mixed).grand_total

    def test_the_projection_carries_the_same_figures(self, session, mixed, sales, manager):
        """Employee view, portal view and projection: one set of numbers."""
        from portal import projection

        _approve_and_issue(session, mixed, sales, manager)
        quotation_service.change_status(
            session, manager, mixed, QuotationStatus.SENT_TO_CUSTOMER
        )
        mixed.contact_email = "buyer@example.invalid"
        session.flush()
        token, _ = portal_service.issue_token(session, sales, mixed)

        optional = next(i for i in mixed.items if i.inclusion is ItemInclusion.OPTIONAL)
        totals = portal_service.compute_selection_totals(mixed, [optional.id])
        view = projection.build_quote_view(mixed, token, totals, selected_ids=[optional.id])
        snapshot = pricing_snapshot.selected(mixed, [optional.id])

        assert view.totals.grand_total == snapshot.grand_total
        assert view.totals.subtotal == snapshot.subtotal
        assert view.totals.tax_amount == snapshot.tax_amount
        assert view.totals.deposit_due == snapshot.deposit_due
        # And they are Decimals, not formatted strings.
        assert isinstance(view.totals.grand_total, D)


class TestTotalsSummary:
    """What the employee screens show. Three figures, three unmistakable names."""

    def test_the_labels_cannot_be_confused(self, session, mixed):
        rows = pricing_snapshot.totals_summary(mixed)
        labels = [r.label for r in rows]

        assert labels == ["Base Total", "Total with All Options"]
        # No bare "Total": the ambiguity is the thing being designed out.
        assert "Total" not in labels
        assert len(set(labels)) == len(labels)
        # Each carries its own explanation, so the label is never the only clue.
        assert all(r.help_text for r in rows)
        assert len({r.help_text for r in rows}) == len(rows)

    def test_the_all_options_figure_is_derived_not_stored(self, session, mixed):
        rows = {r.label: r.amount for r in pricing_snapshot.totals_summary(mixed)}

        assert rows["Total with All Options"] == pricing_snapshot.all_options(
            mixed
        ).grand_total
        assert rows["Total with All Options"] > rows["Base Total"]
        # Nothing on the quotation holds it: the only stored total is the base.
        stored = {
            value for key, value in vars(mixed).items()
            if isinstance(value, D) and not key.startswith("_")
        }
        assert rows["Total with All Options"] not in stored
        assert mixed.grand_total == rows["Base Total"]

    def test_base_leads_until_somebody_accepts(self, session, mixed):
        rows = pricing_snapshot.totals_summary(mixed)
        primary = [r for r in rows if r.is_primary]
        assert len(primary) == 1
        assert primary[0].label == "Base Total"

    def test_accepted_becomes_the_primary_amount(self, session, mixed, sales, manager):
        _approve_and_issue(session, mixed, sales, manager)
        quotation_service.change_status(
            session, manager, mixed, QuotationStatus.SENT_TO_CUSTOMER
        )
        mixed.contact_email = "buyer@example.invalid"
        session.flush()
        token, _ = portal_service.issue_token(session, sales, mixed)
        optional = next(i for i in mixed.items if i.inclusion is ItemInclusion.OPTIONAL)
        response = portal_service.approve(
            session, token, customer_name="Dana", accepted_terms=True,
            selected_ids=[optional.id],
        )

        rows = pricing_snapshot.totals_summary(mixed, response)
        primary = [r for r in rows if r.is_primary]

        assert len(primary) == 1
        assert primary[0].label == "Accepted Total"
        assert primary[0].amount == response.grand_total
        # It leads the list as well as being flagged, so it reads first.
        assert rows[0].label == "Accepted Total"
        # The other two stay visible, and stay honestly labelled.
        assert [r.label for r in rows[1:]] == ["Base Total", "Total with All Options"]

    def test_the_accepted_row_is_read_not_recomputed(
        self, session, mixed, sales, manager
    ):
        """It must come off the response row, whatever the quotation now says."""
        _approve_and_issue(session, mixed, sales, manager)
        quotation_service.change_status(
            session, manager, mixed, QuotationStatus.SENT_TO_CUSTOMER
        )
        mixed.contact_email = "buyer@example.invalid"
        session.flush()
        token, _ = portal_service.issue_token(session, sales, mixed)
        response = portal_service.approve(
            session, token, customer_name="Dana", accepted_terms=True,
        )

        # Stand in a figure that no scope of this quotation could produce.
        response.grand_total = D("1.23")
        session.flush()

        accepted = pricing_snapshot.totals_summary(mixed, response)[0]
        assert accepted.amount == D("1.23")
        assert accepted.amount != pricing_snapshot.base(mixed).grand_total
        assert accepted.amount != pricing_snapshot.all_options(mixed).grand_total


class TestNoSecondArithmeticPath:
    def test_the_portal_service_holds_no_calculation_tools(self):
        """It delegates, so it must not still import the means to do its own."""
        import inspect

        source = inspect.getsource(portal_service)
        for name in ("compute_totals(", "compute_line(", "LineInput(", "ChargeInput("):
            assert name not in source, f"portal_service still calls {name}"

    def test_the_employee_pages_do_not_add_money_up(self):
        """The pages render figures from the snapshot; they never build one.

        Word-boundary matching on purpose: ``recompute_totals`` is the service
        call that *delegates* to the engine and is exactly what a page should
        use. A bare ``compute_totals(`` is the page doing the sum itself.
        """
        import re
        from pathlib import Path

        for page in ("pages/12_Customer_Portal.py", "pages/02_Create_Quotation.py"):
            source = Path(page).read_text(encoding="utf-8")
            for pattern in (
                r"\bcompute_totals\s*\(", r"\bcompute_line\s*\(",
                r"net_line_total\s+for", r"\bsum\s*\(",
            ):
                assert not re.search(pattern, source), (
                    f"{page} computes money itself: {pattern}"
                )

    def test_every_summary_figure_traces_to_the_snapshot(self, session, mixed):
        rows = {r.label: r.amount for r in pricing_snapshot.totals_summary(mixed)}
        assert rows == {
            "Base Total": pricing_snapshot.price(
                mixed, PriceScope.BASE
            ).grand_total,
            "Total with All Options": pricing_snapshot.price(
                mixed, PriceScope.ALL_OPTIONS
            ).grand_total,
        }


class TestAcceptedTotalIsImmutable:
    def test_acceptance_records_the_selected_scope(self, session, mixed, sales, manager):
        _approve_and_issue(session, mixed, sales, manager)
        quotation_service.change_status(
            session, manager, mixed, QuotationStatus.SENT_TO_CUSTOMER
        )
        mixed.contact_email = "buyer@example.invalid"
        session.flush()
        token, _ = portal_service.issue_token(session, sales, mixed)
        optional = next(i for i in mixed.items if i.inclusion is ItemInclusion.OPTIONAL)
        expected = pricing_snapshot.selected(mixed, [optional.id])

        response = portal_service.approve(
            session, token, customer_name="Dana", accepted_terms=True,
            selected_ids=[optional.id],
        )
        assert response.grand_total == expected.grand_total
        assert response.grand_total != pricing_snapshot.base(mixed).grand_total
        assert response.selected_item_ids == [optional.id]

    def test_the_accepted_total_is_not_recomputed_later(
        self, session, mixed, sales, manager
    ):
        """A later price change must not restate what somebody agreed to."""
        _approve_and_issue(session, mixed, sales, manager)
        quotation_service.change_status(
            session, manager, mixed, QuotationStatus.SENT_TO_CUSTOMER
        )
        mixed.contact_email = "buyer@example.invalid"
        session.flush()
        token, _ = portal_service.issue_token(session, sales, mixed)
        response = portal_service.approve(
            session, token, customer_name="Dana", accepted_terms=True,
        )
        agreed = response.grand_total

        # The accepted total cannot drift because the quotation cannot change:
        # acceptance locks the revision, and the immutability guard refuses an
        # edit outright rather than relying on anyone remembering not to.
        from modules.models import ImmutableRecordError

        assert mixed.is_locked is True
        with pytest.raises(ImmutableRecordError):
            mixed.tax_rate_pct = D("25")
            session.flush()
        session.rollback()

        assert response.grand_total == agreed
