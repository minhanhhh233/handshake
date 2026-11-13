"""Fill algorithm, document 09 section 2.3 (F-01 to F-12), against the real shortlist catalog.

Upstream is always a FakeUpstream. F-10 (gateway substitutes) is deferred with T-404.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from handshake import db, timeutil, upstream
from handshake.errors import HandshakeError
from handshake.models import Mandate, OfferRequest, ProductFacts
from handshake.offers import build_offer, mandate_product_reasons

from .factories import make_party, mandate_body
from .fakes import FakeUpstream, make_facts


def delivery():
    return timeutil.today_helsinki() + timedelta(days=2)


def facts(pid, **kw):
    kw.setdefault("available_date", delivery())
    kw.setdefault("closing", timeutil.now() + timedelta(days=1))
    return make_facts(pid, **kw)


@pytest.fixture
def fake():
    holder = {}

    def install(*fs, fail=None):
        holder["f"] = FakeUpstream(list(fs), fail=fail)
        upstream.set_adapter(holder["f"])
        return holder["f"]

    yield install
    upstream.set_adapter(None)


def request(product_ids, value=1000, unit="L", **line):
    return OfferRequest.model_validate({
        "delivery_date": delivery().isoformat(),
        "lines": [{"product_ids": product_ids, "quantity": {"value": value, "unit": unit},
                   **line}],
    })


def build(party, req, trace="trc_" + "a" * 20):
    return build_offer(req, agent_id=party.agent_id, customer_id=party.customer_id,
                       trace_id=trace)


def add_hold(pid, qty, status="held", expires_in=timedelta(minutes=10)):
    with db.transaction() as conn:
        db.q(conn, "INSERT INTO stock_holds (offer_id, line_no, product_id, delivery_date, "
                   "quantity_sales_units, status, expires_at) VALUES ('off_x', 1, :p, :d, :q, "
                   ":s, :e)", p=pid, d=delivery(), q=qty, s=status,
             e=timeutil.to_db(timeutil.now() + expires_in) if status == "held" else None)


pytestmark = [pytest.mark.db]


def test_f01_requested_has_full_capacity(catalog, clean_db, fake):
    fake(facts("464306", amount=None))
    out = build(make_party(), request(["464306", "464398"]))
    offer = out["options"][0]["offer"]
    assert len(offer["lines"]) == 1
    ln = offer["lines"][0]
    assert ln["role"] == "requested" and ln["quantity"] == {"value": 1000, "unit": "SALES_UNIT"}
    assert ln["availability"]["assurance"] == "confirmed"
    assert offer["fulfilment"][0]["complete"] is True and offer["fulfilment"][0]["split"] is False


def test_f02_split_requested_then_alternate(catalog, clean_db, fake):
    fake(facts("469363", price="2.48", comparison="2.48", amount="600"),
         facts("462775", price="2.57", comparison="2.57", amount="500"))
    offer = build(make_party(), request(["469363", "462775"]))["options"][0]["offer"]
    assert [(ln["product_id"], ln["role"], ln["quantity"]["value"]) for ln in offer["lines"]] == [
        ("469363", "requested", 600), ("462775", "alternate", 400)]
    fs = offer["fulfilment"][0]
    assert fs["complete"] is True and fs["split"] is True
    assert fs["offered"] == {"value": 1000, "unit": "L"}
    assert offer["totals"]["excl_tax"] == "2516.00"  # 600*2.48 + 400*2.57


def test_f03_exact_uses_first_candidate_only(catalog, clean_db, fake):
    fake(facts("469363", price="2.48", comparison="2.48", amount="600"),
         facts("462775", price="2.57", comparison="2.57", amount="500"))
    req = request(["469363", "462775"], fulfilment="exact")
    offer = build(make_party(), req)["options"][0]["offer"]
    assert len(offer["lines"]) == 1 and offer["lines"][0]["quantity"]["value"] == 600
    assert offer["fulfilment"][0]["complete"] is False
    assert offer["fulfilment"][0]["offered"]["value"] == 600


def test_f04_terminal_is_unconfirmed_full_quantity(catalog, clean_db, fake):
    fake(facts("465039", price="6.86", comparison="1.37", logistic="terminal", amount=None,
               sales_unit="KI"))
    offer = build(make_party(), request(["465039"]))["options"][0]["offer"]
    ln = offer["lines"][0]
    assert ln["availability"]["assurance"] == "unconfirmed"
    assert ln["conversion"] == {"base_per_sales_unit": 5.0073, "method": "price_ratio"}
    assert ln["quantity"]["value"] == 200 and ln["sales_unit"] == "KI"
    assert offer["fulfilment"][0]["complete"] is True


def test_f05_later_available_date_excluded_alternate_used(catalog, clean_db, fake):
    late = delivery() + timedelta(days=3)
    fake(facts("464398", available_date=late), facts("464306", amount=None))
    out = build(make_party(), request(["464398", "464306"]))
    ex = out["excluded"]
    assert ex[0]["product_id"] == "464398" and ex[0]["reason"] == "not_deliverable_by_date"
    assert ex[0]["available_date"] == late.isoformat() and ex[0]["order_by"] is not None
    assert ex[0]["request_line"] == 1
    lines = out["options"][0]["offer"]["lines"]
    assert [(ln["product_id"], ln["role"]) for ln in lines] == [("464306", "alternate")]


def test_f06_sales_block(catalog, clean_db, fake):
    fake(facts("464398", sales_block=True), facts("464306", amount=None))
    out = build(make_party(), request(["464398", "464306"]))
    assert out["excluded"][0]["reason"] == "sales_blocked"


def test_f07_active_hold_reduces_capacity(catalog, clean_db, fake):
    add_hold("469363", 300)
    fake(facts("469363", price="2.48", comparison="2.48", amount="600"))
    offer = build(make_party(), request(["469363"]))["options"][0]["offer"]
    assert offer["lines"][0]["quantity"]["value"] == 300
    assert offer["fulfilment"][0]["complete"] is False


def test_f07b_committed_hold_counts(catalog, clean_db, fake):
    add_hold("469363", 100, status="committed")
    fake(facts("469363", price="2.48", comparison="2.48", amount="600"))
    offer = build(make_party(), request(["469363"]))["options"][0]["offer"]
    assert offer["lines"][0]["quantity"]["value"] == 500


def test_f08_expired_hold_ignored(catalog, clean_db, fake):
    add_hold("469363", 300, expires_in=timedelta(minutes=-1))
    fake(facts("469363", price="2.48", comparison="2.48", amount="600"))
    offer = build(make_party(), request(["469363"]))["options"][0]["offer"]
    assert offer["lines"][0]["quantity"]["value"] == 600


def test_f09_all_excluded(catalog, clean_db, fake):
    fake(facts("464398", price=None), facts("464306", has_schedule=False),
         facts("464201", amount="0"))
    with pytest.raises(HandshakeError) as e:
        build(make_party(), request(["464398", "464306", "464201"]))
    assert e.value.code == "no_candidates" and e.value.status == 422
    reasons = [(x["product_id"], x["reason"]) for x in e.value.extra["excluded"]]
    assert reasons == [("464398", "price_unavailable"), ("464306", "out_of_stock"),
                       ("464201", "out_of_stock")]
    with db.connection() as conn:
        assert db.q(conn, "SELECT COUNT(*) FROM offers").scalar_one() == 0


@pytest.mark.skip(reason="T-404 (gateway substitutes) deferred, document 08 section 0")
def test_f10_official_substitute():
    pass


def test_f11_fails_requirements_before_upstream(catalog, clean_db, fake):
    f = fake(facts("464306", amount=None))
    out = build(make_party(), request(["464398", "464306"],
                                      requirements={"required_claims": ["FREE_FROM_LACTOSE"]}))
    assert out["options"]
    f2 = fake(facts("464306"))
    with pytest.raises(HandshakeError) as e:
        build(make_party(), request(["464398", "464306"],
                                    requirements={"required_claims": ["VEGAN"]}))
    ex = e.value.extra["excluded"]
    assert {x["reason"] for x in ex} == {"fails_requirements"}
    assert f2.calls == [] and len(f.calls) == 1


def test_f11b_requirement_forbidden_allergen(catalog, clean_db, fake):
    f = fake()
    with pytest.raises(HandshakeError) as e:
        build(make_party(), request(["464306"], requirements={"forbidden_allergens": ["AM"]}))
    assert e.value.extra["excluded"][0]["reason"] == "fails_requirements"
    assert f.calls == []


def test_f12_mandate_forbids_milk(catalog, clean_db, fake):
    f = fake()
    party = make_party(forbidden_allergens={"codes": ["AM"]})
    with pytest.raises(HandshakeError) as e:
        build(party, request(["464306", "464398"]))
    ex = e.value.extra["excluded"]
    assert [x["reason"] for x in ex] == ["forbidden_by_mandate"] * 2
    pr = ex[0]["policy_reason"]
    assert pr["rule"] == "R3" and pr["code"] == "forbidden_allergen"
    assert pr["effect"] == "rejected" and "line_no" not in pr
    assert pr["details"] == {"product_id": "464306", "allergen": "AM", "state": "CONTAINS"}
    assert "Lactose-free is not milk-free." in pr["message"]
    assert f.calls == []


def test_two_lines_share_capacity_and_one_upstream_call(catalog, clean_db, fake):
    f = fake(facts("469363", price="2.48", comparison="2.48", amount="600"))
    req = OfferRequest.model_validate({
        "delivery_date": delivery().isoformat(),
        "lines": [{"product_ids": ["469363"], "quantity": {"value": 400, "unit": "L"}},
                  {"product_ids": ["469363"], "quantity": {"value": 400, "unit": "L"}}],
    })
    offer = build(make_party(), req)["options"][0]["offer"]
    assert [ln["quantity"]["value"] for ln in offer["lines"]] == [400, 200]
    assert [fs["complete"] for fs in offer["fulfilment"]] == [True, False]
    assert len(f.calls) == 1 and f.calls[0][0] == ["469363"]


def test_passed_cutoff_excluded_with_null_date(catalog, clean_db, fake):
    fake(facts("465039", logistic="terminal", amount=None,
               closing=timeutil.now() - timedelta(hours=1)), facts("464306", amount=None))
    out = build(make_party(), request(["465039", "464306"]))
    ex = out["excluded"][0]
    assert ex["reason"] == "not_deliverable_by_date" and ex["available_date"] is None
    assert ex["order_by"] is not None and "passed" in ex["message"]


def test_expires_at_capped_by_order_by(catalog, clean_db, fake):
    closing = timeutil.now() + timedelta(minutes=5)
    fake(facts("464306", amount=None, closing=closing))
    offer = build(make_party(), request(["464306"]))["options"][0]["offer"]
    assert offer["expires_at"] == timeutil.iso(closing)


def test_sales_unit_request(catalog, clean_db, fake):
    fake(facts("464306", amount=None))
    offer = build(make_party(), request(["464306"], value=24, unit="SALES_UNIT",
                                        fulfilment="exact"))["options"][0]["offer"]
    ln = offer["lines"][0]
    assert ln["quantity"]["value"] == 24 and ln["base_quantity"] is None
    assert ln["conversion"] is None
    assert offer["fulfilment"][0]["offered"] == {"value": 24, "unit": "SALES_UNIT"}


# ---------------------------------------------------------------- pure mandate checks


def _mandate(**over):
    return Mandate(**mandate_body(**over).model_dump(), mandate_id="mnd_" + "a" * 20, version=1,
                   agent_id="agt_" + "a" * 20, customer_id="cus_" + "a" * 20)


def _facts(**kw):
    base = dict(product_id="p1", category_code="10400", claims=["FREE_FROM_LACTOSE"],
                allergens={"AM": "CONTAINS"}, has_allergen_data=True)
    return ProductFacts(**{**base, **kw})


def test_mandate_reasons_r2_r3_r4():
    assert mandate_product_reasons(_facts(), _mandate()) == []
    r = mandate_product_reasons(_facts(), _mandate(allowed_categories=["10430"]))
    assert [x.code for x in r] == ["category_not_allowed"]
    r = mandate_product_reasons(_facts(), _mandate(forbidden_allergens={"codes": ["AM"]}))
    assert r[0].message.endswith("Lactose-free is not milk-free.")
    r = mandate_product_reasons(_facts(claims=[]), _mandate(forbidden_allergens={"codes": ["AM"]}))
    assert "Lactose-free" not in r[0].message
    may = _facts(allergens={"AN": "MAY_CONTAIN"})
    assert mandate_product_reasons(may, _mandate(forbidden_allergens={
        "codes": ["AN"], "states": ["CONTAINS"]})) == []
    for unknown, effect in (("reject", "rejected"), ("needs_approval", "needs_approval")):
        r = mandate_product_reasons(_facts(), _mandate(forbidden_allergens={
            "codes": ["AN"], "unknown": unknown}))
        assert [(x.code, x.effect) for x in r] == [("allergen_data_missing", effect)]
    assert mandate_product_reasons(_facts(), _mandate(forbidden_allergens={
        "codes": ["AN"], "unknown": "allow"})) == []
    assert mandate_product_reasons(_facts(allergens={"AN": "FREE_FROM"}), _mandate(
        forbidden_allergens={"codes": ["AN"]})) == []

