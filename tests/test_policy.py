"""Policy engine unit tests P-01..P-26 (document 09, section 2.1). Pure: no database."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from handshake.models import (
    ClaimSource,
    ClaimsSource,
    Comparison,
    Differences,
    FulfilmentSummary,
    LineAttributes,
    LineAvailability,
    Mandate,
    Offer,
    OfferLine,
    PriceValid,
    ProductFacts,
    Quantity,
    Requirements,
    Totals,
)
from handshake.policy import evaluate, mandate_product_reasons
from handshake.timeutil import HELSINKI

PID = "464398"
MONDAY = date(2026, 11, 16)
SUNDAY = date(2026, 11, 22)
NOW = datetime(2026, 11, 14, 10, 0, tzinfo=HELSINKI)


def _mandate(**over: Any) -> Mandate:
    doc: dict[str, Any] = {
        "mandate_id": "mnd_test", "version": 1, "agent_id": "agt_test",
        "customer_id": "cus_test",
        "valid_from": "2026-11-01T00:00:00+02:00", "valid_to": None,
        "approvers": [{"name": "Maria Virtanen"}],
        "limits": {"currency": "EUR", "max_order_total_excl_tax": "1000.00"},
        "allowed_categories": None,
        "forbidden_allergens": {"codes": [], "states": ["CONTAINS", "MAY_CONTAIN"],
                                "unknown": "reject"},
        "substitution": {"auto_approve": "official_rules_only"},
        "delivery": {"allowed_weekdays": [1, 2, 3, 4, 5, 6]},
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(doc.get(k), dict):
            doc[k] = {**doc[k], **v}
        else:
            doc[k] = v
    return Mandate.model_validate(doc)


def _line(role: str = "requested", source: str | None = None, total: str = "500.00",
          line_no: int = 1, product_id: str = PID) -> OfferLine:
    diffs = None
    if role != "requested":
        diffs = Differences(
            price_per_base_unit_percent=None, claims_lost=[], claims_gained=[], labels_lost=[],
            labels_gained=[], origin_changed=False, delivery_reliability_delta=None,
            source=source,  # type: ignore[arg-type]
        )
    return OfferLine(
        line_no=line_no, request_line=1, product_id=product_id, name="Test milk",
        role=role,  # type: ignore[arg-type]
        substitute_for="111111" if role == "substitute" else None,
        quantity=Quantity(value=Decimal(1), unit="SALES_UNIT"), sales_unit="PCE",
        base_quantity=None, conversion=None, unit_price_excl_tax=total,
        comparison_price=None, tax_rate_percent="14.00", line_total_excl_tax=total,
        line_total_incl_tax=total, price_valid=PriceValid(),
        availability=LineAvailability(available_date=MONDAY, order_by=None,
                                      logistic_model="stock", assurance="confirmed"),
        attributes=LineAttributes(claims=[], allergens=[], allergen_data="present", labels=[],
                                  country_of_origin="FI"),
        comparison=Comparison(), differences=diffs,
    )


def _offer(total: str = "500.00", delivery: date = MONDAY, **line_kw: Any) -> Offer:
    return Offer(
        offer_id="off_test", issuer="https://handshake.test", agent_id="agt_test",
        customer_id="cus_test", mandate_id="mnd_test", mandate_version=1,
        issued_at=NOW, expires_at=datetime(2026, 11, 14, 10, 15, tzinfo=HELSINKI),
        delivery_date=delivery, lines=[_line(total=total, **line_kw)],
        fulfilment=[FulfilmentSummary(request_line=1,
                                      requested=Quantity(value=Decimal(1), unit="SALES_UNIT"),
                                      offered=Quantity(value=Decimal(1), unit="SALES_UNIT"),
                                      complete=True, split=False)],
        totals=Totals(excl_tax=total, tax="0.00", incl_tax=total),
        claims_source=ClaimsSource(price=ClaimSource(source="p"),
                                   availability=ClaimSource(source="a"),
                                   attributes=ClaimSource(source="g")),
    )


def _facts(**over: Any) -> dict[str, ProductFacts]:
    d: dict[str, Any] = {"product_id": PID, "category_code": "10400", "claims": [],
                         "allergens": {}, "has_allergen_data": True}
    d.update(over)
    return {PID: ProductFacts(**d)}


def _eval(offer: Offer | None = None, mandate: Mandate | None = None, status: str = "active",
          facts: dict[str, ProductFacts] | None = None, now: datetime = NOW,
          requirements: dict[int, Requirements] | None = None,
          week_total: Decimal = Decimal(0)):  # noqa: ANN202
    return evaluate(offer or _offer(), mandate or _mandate(), status, facts or _facts(),
                    week_total, now, requirements)


def _codes(ev) -> list[str]:  # noqa: ANN001
    return [r.code for r in ev.reasons]


# ---------------------------------------------------------------- P-01..P-26


def test_p01_base_accepted() -> None:
    ev = _eval()
    assert ev.outcome == "accepted" and ev.reasons == []


def test_p02_total_equal_to_cap_accepted() -> None:
    ev = _eval(_offer("1000.00"))
    assert ev.outcome == "accepted" and ev.reasons == []


def test_p03_total_over_cap_needs_approval() -> None:
    ev = _eval(_offer("1000.01"))
    assert ev.outcome == "needs_approval" and _codes(ev) == ["spend_cap_exceeded"]
    assert ev.reasons[0].rule == "R6" and ev.reasons[0].line_no is None


def test_r6_message_text() -> None:
    ev = _eval(_offer("1070.00"))
    r = ev.reasons[0]
    assert r.message == "Order total 1070.00 EUR excl. tax exceeds the mandate cap of 1000.00 EUR."
    assert r.details == {"total_excl_tax": "1070.00", "cap": "1000.00", "currency": "EUR"}


def test_r6_no_cap() -> None:
    m = _mandate(limits={"max_order_total_excl_tax": None})
    assert _eval(_offer("99999.00"), m).outcome == "accepted"


def test_p04_agent_suspended() -> None:
    ev = _eval(status="suspended")
    assert ev.outcome == "rejected" and _codes(ev) == ["mandate_inactive"]
    assert ev.reasons[0].rule == "R1"


def test_p05_mandate_expired() -> None:
    ev = _eval(mandate=_mandate(valid_to="2026-11-10T00:00:00+02:00"))
    assert ev.outcome == "rejected" and _codes(ev) == ["mandate_inactive"]


def test_r1_window_edges() -> None:
    assert _eval(mandate=_mandate(valid_to=NOW.isoformat())).outcome == "rejected"
    assert _eval(mandate=_mandate(valid_from="2026-11-15T00:00:00+02:00")).outcome == "rejected"
    assert _eval(mandate=_mandate(valid_from=NOW.isoformat())).outcome == "accepted"


def test_p06_category_not_allowed() -> None:
    ev = _eval(mandate=_mandate(allowed_categories=["10430"]))
    assert ev.outcome == "rejected" and _codes(ev) == ["category_not_allowed"]
    assert ev.reasons[0].line_no == 1


def test_p07_forbidden_allergen_contains() -> None:
    ev = _eval(mandate=_mandate(forbidden_allergens={"codes": ["AM"]}),
               facts=_facts(allergens={"AM": "CONTAINS"}))
    assert ev.outcome == "rejected" and _codes(ev) == ["forbidden_allergen"]
    r = ev.reasons[0]
    assert r.rule == "R3" and r.line_no == 1
    assert r.details == {"product_id": PID, "allergen": "AM", "state": "CONTAINS"}
    assert "Lactose-free" not in r.message


def test_p08_may_contain_in_states() -> None:
    ev = _eval(mandate=_mandate(forbidden_allergens={"codes": ["AN"]}),
               facts=_facts(allergens={"AN": "MAY_CONTAIN"}))
    assert ev.outcome == "rejected" and _codes(ev) == ["forbidden_allergen"]


def test_p09_may_contain_not_in_states() -> None:
    ev = _eval(mandate=_mandate(forbidden_allergens={"codes": ["AN"], "states": ["CONTAINS"]}),
               facts=_facts(allergens={"AN": "MAY_CONTAIN"}))
    assert ev.outcome == "accepted" and ev.reasons == []


def test_p10_free_from() -> None:
    ev = _eval(mandate=_mandate(forbidden_allergens={"codes": ["AN"]}),
               facts=_facts(allergens={"AN": "FREE_FROM"}))
    assert ev.outcome == "accepted" and ev.reasons == []


def test_p11_unknown_reject() -> None:
    ev = _eval(mandate=_mandate(forbidden_allergens={"codes": ["AN"], "unknown": "reject"}))
    assert ev.outcome == "rejected" and _codes(ev) == ["allergen_data_missing"]
    assert ev.reasons[0].effect == "rejected"


def test_p12_unknown_needs_approval() -> None:
    ev = _eval(mandate=_mandate(forbidden_allergens={"codes": ["AN"],
                                                     "unknown": "needs_approval"}))
    assert ev.outcome == "needs_approval" and _codes(ev) == ["allergen_data_missing"]


def test_p13_unknown_allow() -> None:
    ev = _eval(mandate=_mandate(forbidden_allergens={"codes": ["AN"], "unknown": "allow"}))
    assert ev.outcome == "accepted" and ev.reasons == []


def test_no_allergen_data_is_unknown_for_every_code() -> None:
    ev = _eval(mandate=_mandate(forbidden_allergens={"codes": ["AM", "AN"]}),
               facts=_facts(allergens={"AM": "FREE_FROM"}, has_allergen_data=False))
    assert _codes(ev) == ["allergen_data_missing", "allergen_data_missing"]
    assert [r.details["allergen"] for r in ev.reasons] == ["AM", "AN"]


def test_p14_substitute_official_rule() -> None:
    ev = _eval(_offer(role="substitute", source="valio_aimo_substitution_rule"))
    assert ev.outcome == "accepted" and ev.reasons == []


def test_p15_substitute_same_category() -> None:
    ev = _eval(_offer(role="substitute", source="same_category_same_claims"))
    assert ev.outcome == "needs_approval" and _codes(ev) == ["substitution_needs_approval"]
    assert ev.reasons[0].rule == "R8" and ev.reasons[0].line_no == 1


def test_p16_any_meeting_requirements() -> None:
    ev = _eval(_offer(role="substitute", source="same_category_same_claims"),
               _mandate(substitution={"auto_approve": "any_meeting_requirements"}))
    assert ev.outcome == "accepted" and ev.reasons == []


def test_p17_official_rule_with_none() -> None:
    ev = _eval(_offer(role="substitute", source="valio_aimo_substitution_rule"),
               _mandate(substitution={"auto_approve": "none"}))
    assert ev.outcome == "needs_approval" and _codes(ev) == ["substitution_needs_approval"]


def test_p18_alternate_never_fires() -> None:
    for mode in ("none", "official_rules_only", "any_meeting_requirements"):
        ev = _eval(_offer(role="alternate"), _mandate(substitution={"auto_approve": mode}))
        assert ev.outcome == "accepted" and ev.reasons == []


def test_p19_sunday() -> None:
    ev = _eval(_offer(delivery=SUNDAY))
    assert ev.outcome == "needs_approval" and _codes(ev) == ["delivery_day_not_allowed"]
    assert "Sunday" in ev.reasons[0].message and ev.reasons[0].rule == "R9"


def test_p20_cap_and_forbidden_allergen_both_reported() -> None:
    ev = _eval(_offer("1000.01"), _mandate(forbidden_allergens={"codes": ["AM"]}),
               facts=_facts(allergens={"AM": "CONTAINS"}))
    assert ev.outcome == "rejected"
    assert _codes(ev) == ["forbidden_allergen", "spend_cap_exceeded"]


def test_p21_cap_and_sunday() -> None:
    ev = _eval(_offer("1000.01", delivery=SUNDAY))
    assert ev.outcome == "needs_approval"
    assert _codes(ev) == ["spend_cap_exceeded", "delivery_day_not_allowed"]


def test_p22_requirement_not_met() -> None:
    ev = _eval(requirements={1: Requirements(required_claims=["ORGANIC"])})
    assert ev.outcome == "rejected" and _codes(ev) == ["requirement_not_met"]
    r = ev.reasons[0]
    assert r.rule == "R5" and r.line_no == 1 and r.details["missing_claims"] == ["ORGANIC"]


def test_r5_forbidden_allergen_requirement() -> None:
    req = {1: Requirements(forbidden_allergens=["AM"])}
    assert _eval(facts=_facts(allergens={"AM": "FREE_FROM"}), requirements=req).outcome == \
        "accepted"
    for st in ("CONTAINS", "MAY_CONTAIN"):
        ev = _eval(facts=_facts(allergens={"AM": st}), requirements=req)
        assert _codes(ev) == ["requirement_not_met"]
    assert _codes(_eval(requirements=req)) == ["requirement_not_met"]
    met = {1: Requirements(required_claims=["ORGANIC"])}
    assert _eval(facts=_facts(claims=["ORGANIC"]), requirements=met).outcome == "accepted"


@pytest.mark.skip(reason="cut: T-503")
def test_p23_unconfirmed_stock() -> None:
    pass


def test_p24_period_cap() -> None:
    """Weekly cap 1500.00; week total 1200.00; this order 500.00 -> needs_approval (R7)."""
    m = _mandate(limits={"max_weekly_total_excl_tax": "1500.00"})
    ev = _eval(_offer("500.00"), m, week_total=Decimal("1200.00"))
    assert ev.outcome == "needs_approval" and _codes(ev) == ["period_cap_exceeded"]
    r = ev.reasons[0]
    assert r.rule == "R7" and r.effect == "needs_approval" and r.line_no is None
    assert r.details["projected_total_excl_tax"] == "1700.00" and r.details["cap"] == "1500.00"
    assert "1700.00 EUR, above the weekly cap of 1500.00 EUR" in r.message


def test_r7_boundaries() -> None:
    m = _mandate(limits={"max_weekly_total_excl_tax": "1500.00"})
    assert _eval(_offer("500.00"), m, week_total=Decimal("1000.00")).outcome == "accepted"
    assert _eval(_offer("500.00"), m, week_total=Decimal("1000.01")).outcome == "needs_approval"
    # No weekly cap: R7 never fires, whatever the week total.
    assert _eval(_offer("500.00"), week_total=Decimal("99999")).outcome == "accepted"
    # R6 and R7 together are both reported.
    ev = _eval(_offer("1000.01"), m, week_total=Decimal("600.00"))
    assert _codes(ev) == ["spend_cap_exceeded", "period_cap_exceeded"]


def test_p25_deterministic() -> None:
    args = dict(offer=_offer("1000.01", delivery=SUNDAY, role="substitute",
                             source="same_category_same_claims"),
                mandate=_mandate(forbidden_allergens={"codes": ["AM", "AN"]},
                                 allowed_categories=["10430"]),
                facts=_facts(allergens={"AM": "CONTAINS"}, claims=["FREE_FROM_LACTOSE"]),
                requirements={1: Requirements(required_claims=["ORGANIC"])})
    a, b = _eval(**args), _eval(**args)
    assert a == b and a.dump() == b.dump()
    assert [r.rule for r in a.reasons] == ["R2", "R3", "R4", "R5", "R6", "R8", "R9"]


def test_p26_lactose_free_is_not_milk_free() -> None:
    ev = _eval(mandate=_mandate(forbidden_allergens={"codes": ["AM"]}),
               facts=_facts(allergens={"AM": "CONTAINS"}, claims=["FREE_FROM_LACTOSE"]))
    assert ev.outcome == "rejected"
    assert ev.reasons[0].message.endswith("Lactose-free is not milk-free.")


# ---------------------------------------------------------------- extras


def test_ignored_cut_fields() -> None:
    """R10 stays cut: require_confirmed is stored but not evaluated (R7 was restored)."""
    m = _mandate(stock={"require_confirmed": True})
    ev = evaluate(_offer(), m, "active", _facts(), Decimal("0"), NOW)
    assert ev.outcome == "accepted"


def test_reasons_ordered_by_rule_then_line() -> None:
    o = _offer()
    o = o.model_copy(update={"lines": [_line(line_no=2, role="substitute",
                                             source="same_category_same_claims"),
                                       _line(line_no=1)]})
    ev = _eval(o, _mandate(allowed_categories=["10430"]))
    assert [(r.rule, r.line_no) for r in ev.reasons] == [("R2", 1), ("R2", 2), ("R8", 2)]


def test_missing_facts_fail_closed() -> None:
    ev = _eval(facts={}, mandate=_mandate(forbidden_allergens={"codes": ["AM"]}))
    assert ev.outcome == "rejected" and _codes(ev) == ["allergen_data_missing"]


def test_offers_reexport() -> None:
    from handshake import offers
    assert offers.mandate_product_reasons is mandate_product_reasons
