"""The step order and what each step insists on.

These are quotation rules, not widget behaviour, so they are tested without a
Streamlit runtime. The page's use of them is covered in test_app_shell.
"""
from __future__ import annotations

import datetime as dt

import pytest

from modules import wizard


class TestOrder:
    def test_the_sequence_is_the_one_people_work_in(self):
        assert wizard.SEQUENCE == (
            "details", "lines", "shipping", "charges", "terms", "review",
        )

    def test_customer_response_is_not_part_of_the_build(self):
        """It records what happened after sending. Walking through it while
        preparing a quotation would be backwards."""
        assert "tracking" in wizard.STEP_KEYS
        assert "tracking" not in wizard.SEQUENCE

    def test_each_step_leads_to_the_next(self):
        assert wizard.next_step("details") == "lines"
        assert wizard.next_step("lines") == "shipping"
        assert wizard.next_step("shipping") == "charges"
        assert wizard.next_step("charges") == "terms"
        assert wizard.next_step("terms") == "review"

    def test_the_last_step_continues_nowhere(self):
        assert wizard.next_step("review") is None
        assert wizard.is_last("review") is True
        assert wizard.is_last("details") is False

    def test_back_walks_the_sequence_in_reverse(self):
        assert wizard.previous_step("review") == "terms"
        assert wizard.previous_step("lines") == "details"

    def test_the_first_step_goes_back_nowhere(self):
        assert wizard.previous_step("details") is None

    def test_a_step_outside_the_sequence_continues_nowhere(self):
        assert wizard.next_step("tracking") is None
        assert wizard.previous_step("tracking") is None

    def test_an_unknown_step_is_refused_rather_than_guessed(self):
        with pytest.raises(wizard.UnknownStep):
            wizard.next_step("invoicing")

    def test_a_stale_step_key_falls_back_to_the_start(self):
        """Session state outlives the code that wrote it."""
        assert wizard.resolve_step("a_step_that_was_renamed") == "details"
        assert wizard.resolve_step(None) == "details"
        assert wizard.resolve_step("terms") == "terms"


def _details(**over):
    base = {
        "customer_id": 4,
        "currency": "USD",
        "quote_date": dt.date(2026, 8, 1),
        "valid_until": dt.date(2026, 9, 1),
    }
    base.update(over)
    return {"header": base}


class TestDetailsValidation:
    def test_a_complete_header_advances(self):
        assert wizard.validate("details", _details()) == []
        assert wizard.may_advance("details", _details()) is True

    def test_a_missing_customer_blocks(self):
        problems = wizard.validate("details", _details(customer_id=None))
        assert any("customer" in p.lower() for p in problems)

    def test_a_missing_currency_blocks(self):
        assert wizard.validate("details", _details(currency="")) != []

    def test_expiring_before_being_issued_blocks(self):
        problems = wizard.validate("details", _details(
            quote_date=dt.date(2026, 9, 1), valid_until=dt.date(2026, 8, 1),
        ))
        assert any("before" in p.lower() for p in problems)

    def test_the_same_day_is_allowed(self):
        """A quotation valid only on the day it is issued is unusual, not wrong."""
        day = dt.date(2026, 8, 1)
        assert wizard.validate("details", _details(quote_date=day, valid_until=day)) == []


class TestLinesValidation:
    def test_at_least_one_line_is_required(self):
        problems = wizard.validate("lines", {"lines": []})
        assert any("at least one line" in p.lower() for p in problems)

    def test_a_quotation_with_lines_advances(self):
        assert wizard.validate("lines", {"lines": [{"id": 1}]}) == []

    def test_partly_filled_rows_block_the_advance(self):
        problems = wizard.validate(
            "lines", {"lines": [{"id": 1}], "incomplete_line_count": 2},
        )
        assert any("2 line(s)" in p for p in problems)

    def test_the_block_says_the_row_will_not_be_discarded(self):
        """The rule people need to trust: nothing half-entered vanishes."""
        problems = wizard.validate(
            "lines", {"lines": [{"id": 1}], "incomplete_line_count": 1},
        )
        assert any("discarded" in p.lower() for p in problems)


class TestShippingValidation:
    def test_shipping_is_optional_when_freight_is_not_charged(self):
        assert wizard.validate("shipping", {"freight_charged": False}) == []

    def test_charging_freight_without_containers_blocks(self):
        problems = wizard.validate(
            "shipping", {"freight_charged": True, "container_count": 0},
        )
        assert any("container" in p.lower() for p in problems)

    def test_charging_freight_with_containers_advances(self):
        assert wizard.validate(
            "shipping", {"freight_charged": True, "container_count": 2},
        ) == []


class TestOptionalSteps:
    @pytest.mark.parametrize("step", ["charges", "terms", "review"])
    def test_optional_steps_never_block(self, step):
        """A wizard that refuses to pass an empty optional step teaches people
        to ignore its warnings."""
        assert wizard.validate(step, {}) == []


class TestLocking:
    def test_a_locked_quotation_offers_no_editable_step(self):
        """Navigating to a step must never be what makes it editable."""
        assert wizard.editable_steps(editable=False) == frozenset()

    def test_an_editable_quotation_offers_every_step(self):
        assert wizard.editable_steps(editable=True) == frozenset(wizard.STEP_KEYS)


class TestSessionKeys:
    def test_two_quotations_do_not_share_a_position(self):
        assert wizard.step_state_key(1) != wizard.step_state_key(2)

    def test_the_keys_are_distinct_from_each_other(self):
        keys = {
            wizard.step_state_key(7),
            wizard.dirty_state_key(7),
            wizard.saving_state_key(7),
        }
        assert len(keys) == 3
