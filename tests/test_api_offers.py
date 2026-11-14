"""POST /v1/offers and GET /v1/offers/{offer_id} (document 03, section 5; document 09, 3)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from handshake import db, events, signing, timeutil, upstream
from handshake.config import get_settings
from handshake.errors import upstream_unavailable

from .factories import make_party
from .fakes import FakeUpstream, make_facts

pytestmark = [pytest.mark.db]


def delivery():
    return timeutil.today_helsinki() + timedelta(days=2)


def facts(pid, **kw):
    kw.setdefault("available_date", delivery())
    kw.setdefault("closing", timeutil.now() + timedelta(days=1))
    return make_facts(pid, **kw)


@pytest.fixture
def client():
    from handshake.api import create_app

    yield TestClient(create_app())
    upstream.set_adapter(None)


def fake(*fs, fail=None):
    f = FakeUpstream(list(fs), fail=fail)
    upstream.set_adapter(f)
    return f


def body(product_ids, value=1000, unit="L", **line):
    return {"delivery_date": delivery().isoformat(),
            "lines": [{"product_ids": product_ids, "quantity": {"value": value, "unit": unit},
                       **line}]}


def count(sql, **kw):
    with db.connection() as conn:
        return db.q(conn, sql, **kw).scalar_one()


def test_api05_requires_key(client, clean_db):
    r = client.post("/v1/offers", json=body(["464306"]))
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated"


def test_api06_too_many_ids(client, clean_db):
    p = make_party()
    r = client.post("/v1/offers", json=body([str(460000 + i) for i in range(26)]),
                    headers=p.headers)
    assert r.status_code == 400 and r.json()["code"] == "invalid_request"


def test_invalid_rules(client, catalog, clean_db):
    p = make_party()
    f = fake()
    past = body(["464306"])
    past["delivery_date"] = (timeutil.today_helsinki() - timedelta(days=1)).isoformat()
    cases = [past, body(["464306"], unit="PCS"), body(["464306"], value=0),
             body(["464306", "464398"], unit="SALES_UNIT", fulfilment="exact"),
             body(["464306"], unit="SALES_UNIT"), body(["464306", "999999999"])]
    for b in cases:
        r = client.post("/v1/offers", json=b, headers=p.headers)
        assert r.status_code == 400, (b, r.json())
        assert r.json()["code"] == "invalid_request" and r.json()["errors"]
    assert "999999999" in r.json()["detail"]
    assert f.calls == []


def test_no_mandate_forbidden(client, catalog, clean_db):
    p = make_party()
    with db.transaction() as conn:
        db.q(conn, "DELETE FROM mandates WHERE agent_id = :a", a=p.agent_id)
    r = client.post("/v1/offers", json=body(["464306"]), headers=p.headers)
    assert r.status_code == 403 and r.json()["code"] == "forbidden"


def test_api07_valid_offer_signed_held_and_traced(client, catalog, clean_db):
    p = make_party()
    fake(facts("464306", amount=None))
    r = client.post("/v1/offers", json=body(["464306"]), headers=p.headers)
    assert r.status_code == 201, r.json()
    out = r.json()
    trace = out["trace_id"]
    assert r.headers["X-Trace-Id"] == trace
    opt = out["options"][0]
    assert opt["label"] == "best" and "score" not in opt
    payload = signing.verify(opt["jws"], signing.jwks(), signing.TYP_OFFER)
    assert payload == opt["offer"]
    offer = opt["offer"]
    assert offer["status"] == "active" and offer["agent_id"] == p.agent_id
    assert offer["mandate_id"] == p.mandate.mandate_id and offer["mandate_version"] == 1
    assert count("SELECT COUNT(*) FROM stock_holds WHERE offer_id = :o AND status = 'held'",
                 o=offer["offer_id"]) == 1
    assert count("SELECT COUNT(*) FROM offer_lines WHERE offer_id = :o",
                 o=offer["offer_id"]) == 1
    with db.connection() as conn:
        evs = events.list_events(conn, trace)
    assert [e["type"] for e in evs] == ["offer.requested", "offer.issued"]
    assert evs[0]["actor_type"] == "agent" and evs[1]["actor_type"] == "gateway"
    assert all(e["audience"] == "both" for e in evs)
    assert evs[1]["text"].startswith(f"Issued offer {offer['offer_id']} for 1000 L of 464306, "
                                     f"total 1010.00 EUR excl. tax, valid until ")
    assert events.verify_chain(evs) == (True, None)


def test_realistic_1000_litres_of_464306(client, catalog, clean_db):
    p = make_party()
    fake(facts("464306", price="1.01", comparison="1.01", amount=None))
    r = client.post("/v1/offers", json=body(
        ["464306"], requirements={"required_claims": ["FREE_FROM_LACTOSE"]}), headers=p.headers)
    assert r.status_code == 201, r.json()
    offer = r.json()["options"][0]["offer"]
    ln = offer["lines"][0]
    assert ln["quantity"] == {"value": 1000, "unit": "SALES_UNIT"}
    assert ln["base_quantity"] == {"value": 1000, "unit": "L"}
    assert ln["conversion"] == {"base_per_sales_unit": 1, "method": "price_ratio"}
    assert ln["unit_price_excl_tax"] == "1.01" and ln["tax_rate_percent"] == "13.50"
    assert ln["comparison_price"] == {"value": "1.01", "unit": "L"}
    assert ln["line_total_excl_tax"] == "1010.00"
    assert ln["sales_unit"] == "ST" and ln["role"] == "requested"
    assert ln["availability"]["logistic_model"] == "stock"
    assert ln["availability"]["assurance"] == "confirmed"
    assert ln["attributes"]["allergens"] == [{"code": "AM", "state": "CONTAINS"}]
    assert "FREE_FROM_LACTOSE" in ln["attributes"]["claims"]
    assert ln["attributes"]["allergen_data"] == "present"
    assert ln["comparison"] == {"delivery_reliability": 1.0, "carbon_kg_co2e_per_kg": 1.1,
                                "carbon_basis": "product_group_average"}
    assert ln["differences"] is None and ln["substitute_for"] is None
    assert ln["price_valid"] == {"from": "2026-09-01T00:00:00+0300",
                                 "to": "2026-12-31T00:00:00+0200"}
    t = offer["totals"]
    assert t["excl_tax"] == "1010.00"
    # A-25: 1010.00 x 1.135 rounded once per line; upstream valueWithTax is not used.
    assert ln["line_total_incl_tax"] == "1146.35"
    assert t["tax"] == "136.35" and t["incl_tax"] == "1146.35"
    assert offer["claims_source"]["attributes"] == {
        "source": "gs1_synkka_via_valio_aimo", "assurance": "supplier_declared"}
    assert offer["fulfilment"] == [{"request_line": 1, "requested": {"value": 1000, "unit": "L"},
                                    "offered": {"value": 1000, "unit": "L"},
                                    "complete": True, "split": False}]
    issued = timeutil.parse_upstream(offer["issued_at"])
    expires = timeutil.parse_upstream(offer["expires_at"])
    assert expires - issued == timedelta(seconds=get_settings().offer_ttl_seconds)


def test_api20_upstream_failure(client, catalog, clean_db):
    p = make_party()
    fake(fail=upstream_unavailable())
    r = client.post("/v1/offers", json=body(["464306"]), headers=p.headers,
                    params=None)
    assert r.status_code == 503 and r.json()["code"] == "upstream_unavailable"
    assert count("SELECT COUNT(*) FROM offers") == 0
    trace = r.json()["trace_id"]
    with db.connection() as conn:
        assert [e["type"] for e in events.list_events(conn, trace)] == ["offer.requested"]


def test_no_candidates_not_deliverable(client, catalog, clean_db):
    p = make_party()
    late = delivery() + timedelta(days=4)
    fake(facts("464306", available_date=late),
         facts("465039", logistic="terminal", amount=None,
               closing=timeutil.now() - timedelta(days=1)))
    r = client.post("/v1/offers", json=body(["464306", "465039"]), headers=p.headers)
    assert r.status_code == 422
    j = r.json()
    assert j["code"] == "no_candidates" and j["next"]
    a, b = j["excluded"]
    assert a == {"product_id": "464306", "request_line": 1, "reason": "not_deliverable_by_date",
                 "message": a["message"], "available_date": late.isoformat(),
                 "order_by": a["order_by"]}
    assert a["order_by"] is not None
    assert b["available_date"] is None and b["order_by"] is not None
    assert "no later" in b["message"]
    assert count("SELECT COUNT(*) FROM offers") == 0


def test_api21_milk_allergy_mandate(client, catalog, clean_db):
    p = make_party("Koivula School Kitchens", forbidden_allergens={"codes": ["AM"]})
    f = fake()
    ids = ["464306", "464398", "464201", "469363", "462775", "465039", "463038", "464179"]
    r = client.post("/v1/offers", json=body(
        ids, requirements={"required_claims": ["FREE_FROM_LACTOSE"]}), headers=p.headers)
    assert r.status_code == 422
    ex = r.json()["excluded"]
    assert [x["product_id"] for x in ex] == ids
    assert all(x["reason"] == "forbidden_by_mandate" for x in ex)
    assert all("Lactose-free is not milk-free." in x["policy_reason"]["message"] for x in ex)
    assert f.calls == []


def test_get_own_and_other_agents_offer(client, catalog, clean_db):
    a, b = make_party(), make_party("Other")
    fake(facts("464306", amount=None))
    created = client.post("/v1/offers", json=body(["464306"]), headers=a.headers).json()
    oid = created["options"][0]["offer"]["offer_id"]
    r = client.get(f"/v1/offers/{oid}", headers=a.headers)
    assert r.status_code == 200
    assert r.json() == {"offer": created["options"][0]["offer"],
                        "jws": created["options"][0]["jws"]}
    assert client.get(f"/v1/offers/{oid}", headers=b.headers).status_code == 403
    r = client.get("/v1/offers/off_aaaaaaaaaaaaaaaaaaaa", headers=a.headers)
    assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_get_after_expiry_releases_holds(client, catalog, clean_db):
    p = make_party()
    fake(facts("469363", price="2.48", comparison="2.48", amount="600"))
    created = client.post("/v1/offers", json=body(["469363"], value=100),
                          headers=p.headers).json()
    oid, trace = created["options"][0]["offer"]["offer_id"], created["trace_id"]
    timeutil.shift_clock(timedelta(minutes=16))
    r = client.get(f"/v1/offers/{oid}", headers=p.headers)
    assert r.status_code == 200 and r.json()["offer"]["status"] == "expired"
    payload = signing.verify(r.json()["jws"], signing.jwks(), signing.TYP_OFFER)
    assert payload["status"] == "active"
    assert count("SELECT COUNT(*) FROM stock_holds WHERE offer_id = :o AND status = 'released'",
                 o=oid) == 1
    client.get(f"/v1/offers/{oid}", headers=p.headers)  # second read writes nothing new
    with db.connection() as conn:
        evs = events.list_events(conn, trace)
    assert [(e["type"], e["audience"]) for e in evs] == [
        ("offer.requested", "both"), ("offer.issued", "both"), ("offer.expired", "both"),
        ("holds.changed", "aimo_only")]
    assert events.verify_chain(evs) == (True, None)


def test_supplied_trace_id_is_used(client, catalog, clean_db):
    p = make_party()
    fake(facts("464306", amount=None))
    trace = "trc_" + "b" * 20
    r = client.post("/v1/offers", json=body(["464306"]),
                    headers={**p.headers, "X-Trace-Id": trace})
    assert r.status_code == 201 and r.json()["trace_id"] == trace
    assert count("SELECT COUNT(*) FROM offers WHERE trace_id = :t", t=trace) == 1


def test_substitute_line_signed_with_differences(client, catalog, clean_db):
    p = make_party()
    f = fake(facts("464306", amount="300"),
             facts("464201", price="1.56", comparison="1.56", amount=None))
    r = client.post("/v1/offers", json=body(["464306"], substitution="allowed"),
                    headers=p.headers)
    assert r.status_code == 201, r.json()
    opt = r.json()["options"][0]
    assert signing.verify(opt["jws"], signing.jwks(), signing.TYP_OFFER) == opt["offer"]
    sub = opt["offer"]["lines"][1]
    assert sub["role"] == "substitute" and sub["substitute_for"] == "464306"
    assert set(sub["differences"]) == {
        "price_per_base_unit_percent", "claims_lost", "claims_gained", "labels_lost",
        "labels_gained", "origin_changed", "delivery_reliability_delta", "source"}
    assert len(f.calls) == 1
    assert count("SELECT COUNT(*) FROM stock_holds WHERE offer_id = :o",
                 o=opt["offer"]["offer_id"]) == 2


def test_max_options_three_returns_one(client, catalog, clean_db):
    p = make_party()
    fake(facts("464306", amount=None))
    b = body(["464306"], substitution="allowed")
    b["max_options"] = 3
    r = client.post("/v1/offers", json=b, headers=p.headers)
    assert r.status_code == 201 and len(r.json()["options"]) == 1


def test_t406_offer_line_comparison_omits_missing_keys(client, catalog, clean_db):
    """FR-40: offer lines carry comparison data where it exists and omit keys without data.
    469363 has a carbon value but no delivery reliability in the data set."""
    p = make_party(limits={"max_order_total_excl_tax": "5000.00"})
    fake(facts("469363", price="2.48", comparison="2.48", amount="384"))
    r = client.post("/v1/offers", headers=p.headers, json=body(["469363"], value=100))
    assert r.status_code == 201, r.text
    comp = r.json()["options"][0]["offer"]["lines"][0]["comparison"]
    assert "delivery_reliability" not in comp
    assert comp["carbon_basis"] == "product_group_average" and "carbon_kg_co2e_per_kg" in comp
