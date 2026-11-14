"""One-click scripted scenarios (stage 2; document 09, section 6d), against the real gateway
in-process with a fake upstream."""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from handshake import db, live_demo, public_demo, sandbox, scenarios, timeutil, upstream

from .fakes import FakeUpstream, make_facts
from .test_public_demo import _seed

pytestmark = pytest.mark.db


@pytest.fixture
def world(catalog, clean_db):
    from handshake.api import create_app

    _seed()
    d = timeutil.today_helsinki() + timedelta(days=2)
    closing = timeutil.now() + timedelta(days=1)

    def f(pid, price, comparison=None, unit="L", **kw):
        return make_facts(pid, price=price, comparison=comparison or price,
                          comparison_unit=unit, available_date=d, closing=closing, **kw)

    facts = [f(p, "1.01" if p == "464306" else "1.60", amount=None)
             for p in public_demo.SHORTLIST if p != "469363"]
    facts += [f(p, "1.10", amount=None) for p in ("464401", "464073", "462921")]
    # Coffee for "Compare three offers" (prices, stock as seen live on 2026-11-14).
    facts += [f("467004", "85.21", "14.20", "KG", amount="14"),
              f("468618", "67.18", "15.27", "KG", amount="92"),
              f("462245", "89.15", "14.86", "KG", amount="14"),
              f("464322", "104.52", "17.42", "KG", amount="585"),
              f("464724", "105.78", "17.22", "KG", amount="58")]
    facts += [f("469363", "2.48", amount="384"),
              f("465236", "11.62", "3.87", "KG", amount=None),
              f("470341", "2.10", "8.40", amount=None),
              f("462909", "42.00", "8.40", amount=None),
              make_facts("469288", price="2.00", has_schedule=False),
              make_facts("465039", price="6.86", available_date=d,
                         closing=timeutil.now() - timedelta(hours=1))]
    upstream.set_adapter(FakeUpstream(facts))
    app = create_app()
    c = TestClient(app, follow_redirects=False)
    c.get("/demo/sandbox?persona=hotel", headers={"X-Forwarded-For": "198.51.100.60"})
    sid = sandbox.parse_cookie(c.cookies.get(sandbox.COOKIE))
    yield c, sid, d
    upstream.set_adapter(None)


def run(sid, scenario, persona, ip="198.51.100.61"):
    trace = scenarios.start(scenario, persona, sid, ip, wait=True)
    v = live_demo.view(trace, sandbox_id=sid)
    assert v["steps"][-1]["kind"] == "end", v["steps"][-1]
    assert v["steps"][-1]["detail"]["reason"] == "end_turn", v["steps"][-1]
    return v


def results(v, tool=None):
    return [s for s in v["steps"] if s["kind"] == "result" and (tool is None
                                                                  or s["title"] == tool)]


def test_hotel_milk_goes_to_approval_at_1010(world):
    c, sid, _ = world
    v = run(sid, "hotel_milk", "hotel")
    offer = results(v, "request_offer")[0]["detail"]["options"][0]["offer"]
    assert offer["totals"]["excl_tax"] == "1010.00"
    assert any(s["kind"] == "verify" and s["detail"]["verified"] for s in v["steps"])
    assert v["order"]["status"] == "pending_approval" and "approval_url" in v["order"]


def test_hotel_split_and_twice(world):
    c, sid, _ = world
    v = run(sid, "hotel_split", "hotel")
    lines = results(v, "request_offer")[0]["detail"]["options"][0]["offer"]["lines"]
    assert [(ln["product_id"], ln["quantity"]["value"]) for ln in lines] == [
        ("469363", 384), ("464306", 616)]
    v = run(sid, "hotel_twice", "hotel")
    orders_ = results(v, "place_order")
    assert orders_[0]["detail"]["order_id"] == orders_[1]["detail"]["order_id"]
    assert orders_[2]["status"] == "bad" and orders_[2]["detail"]["code"] == "offer_consumed"


def test_hotel_decline_then_smaller_order(world):
    c, sid, _ = world
    trace = scenarios.start("hotel_decline", "hotel", sid, "198.51.100.62")
    deadline = time.time() + 20
    url = None
    while time.time() < deadline and not url:
        v = live_demo.view(trace, sandbox_id=sid)
        url = (v.get("order") or {}).get("approval_url")
        time.sleep(0.2)
    assert url
    assert c.post(urlparse(url).path, data={"decision": "decline"}).status_code == 303
    for t in threading.enumerate():
        if t.name == f"scenario-{trace}":
            t.join(30)
    v = live_demo.view(trace, sandbox_id=sid)
    offers_ = results(v, "request_offer")
    assert len(offers_) == 2
    second = offers_[1]["detail"]["options"][0]["offer"]
    from decimal import Decimal

    assert Decimal(second["totals"]["excl_tax"]) <= Decimal("1000.00")
    assert second["lines"][0]["quantity"]["value"] == 990
    assert results(v, "place_order")[-1]["detail"]["status"] == "confirmed"


def test_hotel_expire_then_requote(world, monkeypatch):
    c, sid, _ = world
    monkeypatch.setattr(scenarios.time, "sleep",
                        lambda s: timeutil.shift_clock(timedelta(seconds=s)))
    v = run(sid, "hotel_expire", "hotel")
    orders_ = results(v, "place_order")
    assert orders_[0]["detail"]["code"] == "offer_expired"
    assert orders_[1]["detail"]["status"] == "confirmed"


def test_restaurant_scenarios(world):
    c, sid, _ = world
    v = run(sid, "restaurant_weekly", "restaurant")
    last = results(v, "place_order")[-1]["detail"]
    assert last["status"] == "pending_approval"
    assert "period_cap_exceeded" in [r["code"] for r in last["reasons"]]
    v = run(sid, "restaurant_weekday", "restaurant")
    dry = results(v, "dry_run_order")[0]["detail"]
    assert "delivery_day_not_allowed" in [r["code"] for r in dry["reasons"]]
    v = run(sid, "restaurant_substitutes", "restaurant")
    offer = results(v, "request_offer")[0]["detail"]["options"][0]["offer"]
    by_line = {ln["request_line"]: ln for ln in offer["lines"]}
    assert by_line[1]["product_id"] == "470341" and by_line[1]["role"] == "substitute"
    assert by_line[2]["role"] == "substitute"
    dry = results(v, "dry_run_order")[0]["detail"]
    r8 = [r["line_no"] for r in dry["reasons"] if r["code"] == "substitution_needs_approval"]
    assert r8 and all(by_line_no != 1 for by_line_no in r8)


def test_school_refusals_tomorrow_and_visitor(world):
    c, sid, d = world
    v = run(sid, "school_milk", "school")
    ex = results(v, "request_offer")[0]["detail"]["excluded"]
    assert any("Lactose-free is not milk-free." in e["message"] for e in ex)
    v = run(sid, "school_no_allergen_data", "school")
    ex = results(v, "request_offer")[0]["detail"]["excluded"]
    assert ex[0]["policy_reason"]["code"] == "allergen_data_missing"
    v = run(sid, "school_category", "school")
    ex = results(v, "request_offer")[0]["detail"]["excluded"]
    assert ex[0]["policy_reason"]["code"] == "category_not_allowed"
    v = run(sid, "any_tomorrow", "hotel")
    ex = results(v, "request_offer")[0]["detail"]["excluded"]
    assert ex[0]["reason"] == "not_deliverable_by_date" and ex[0]["available_date"] == \
        d.isoformat()
    assert d.isoformat() in [s for s in v["steps"] if s["kind"] == "text"][-1]["summary"]
    v = run(sid, "visitor_search", "visitor")
    assert results(v, "search_products")[0]["status"] == "ok"
    assert results(v, "request_offer")[0]["detail"]["status"] == 401


def test_misbehaving_agent_is_refused_every_time(world):
    c, sid, _ = world
    v = run(sid, "misbehaving", "hotel")
    assert v["persona"] == "restaurant"
    by_tool = {}
    for s in results(v):
        by_tool.setdefault(s["title"], []).append(s)
    assert by_tool["verify_offer"][0]["detail"]["error"] == "signature_verification_failed"
    statuses = {s["title"]: s["detail"].get("status") for s in results(v)
                if s["title"] in ("approve", "set_mandate", "counter_offer")}
    assert statuses["approve"] == 404 and statuses["set_mandate"] == 403
    assert statuses["counter_offer"] in (404, 405)
    raw_orders = [s["detail"] for s in by_tool["place_order"] if s["status"] == "bad"]
    codes = {d.get("code") for d in raw_orders}
    assert "invalid_request" in codes and "offer_consumed" in codes
    discount = [s for s in by_tool["request_offer"] if s["status"] == "bad"][-1]
    assert discount["detail"]["status"] == 400
    pend = [s["detail"] for s in by_tool["place_order"]
            if s["status"] == "ok" and s["detail"].get("status") == "pending_approval"]
    assert pend  # the split was caught by the weekly cap and/or the overspend held
    import json

    assert "hsk_" not in json.dumps(v)


def test_inbox_and_routes(world):
    c, sid, _ = world
    run(sid, "hotel_milk", "hotel")
    inbox = c.get("/demo/approvals").json()["items"]
    assert len(inbox) == 1 and inbox[0]["total_excl_tax"] == "1010.00"
    assert inbox[0]["approve_path"].startswith("/approve/")
    other = TestClient(c.app)
    other.get("/demo/sandbox?persona=hotel", headers={"X-Forwarded-For": "198.51.100.70"})
    assert other.get("/demo/approvals").json()["items"] == []
    r = c.post(inbox[0]["approve_path"], data={"decision": "approve"})
    assert r.status_code == 303 and c.get("/demo/approvals").json()["items"] == []
    s = c.get("/demo/sandbox?persona=restaurant").json()["scenarios"]
    assert [x["id"] for x in s["scripted"]] == ["restaurant_basket", "restaurant_weekday",
                                               "restaurant_substitutes", "restaurant_weekly",
                                               "compare_options", "any_tomorrow",
                                               "misbehaving"]
    assert s["prompts"]
    a = c.get("/demo/sandbox?persona=approver").json()
    assert a["card"]["persona"] == "approver" and a["inbox"] == []
    assert c.post("/demo/scenario", json={"id": "school_milk", "persona": "hotel"}
                  ).status_code == 400
    assert TestClient(c.app).post("/demo/scenario", json={"id": "hotel_milk",
                                                          "persona": "hotel"}).status_code == 409
    r = c.post("/demo/scenario", json={"id": "any_tomorrow", "persona": "hotel"})
    assert r.status_code == 202 and r.json()["trace_id"].startswith("trc_")
    assert db is not None


def test_hospital_and_probation_go_to_approval(world):
    c, sid, _ = world
    v = run(sid, "hospital_unknown", "hospital")
    dry = results(v, "dry_run_order")[0]["detail"]
    assert {r["code"] for r in dry["reasons"]} == {"allergen_data_missing"}
    assert dry["outcome"] == "needs_approval" and v["order"]["status"] == "pending_approval"
    v = run(sid, "hospital_substitute", "hospital")
    dry = results(v, "dry_run_order")[0]["detail"]
    assert "substitution_needs_approval" in [r["code"] for r in dry["reasons"]]
    v = run(sid, "probation_small", "probation")
    dry = results(v, "dry_run_order")[0]["detail"]
    assert [r["code"] for r in dry["reasons"]] == ["spend_cap_exceeded"]
    assert v["order"]["status"] == "pending_approval"
    chips = c.get("/demo/sandbox?persona=hospital").json()["scenarios"]["scripted"]
    assert [x["id"] for x in chips][:2] == ["hospital_unknown", "hospital_substitute"]


def test_ranked_options_preferences_and_rationale(world):
    import json
    from urllib.parse import urlparse

    c, sid, _ = world
    # Coffee under the hotel's default ranking: three distinct options.
    v = run(sid, "compare_options", "hotel")
    opts = results(v, "request_offer")[0]["detail"]["options"]
    assert [o["label"] for o in opts] == ["best", "better", "good"]
    prods = [[ln["product_id"] for ln in o["offer"]["lines"]] for o in opts]
    assert prods[0] == ["468618"]              # best: low carbon (0.79) and cheap per kg
    assert prods[1][0] == "467004"             # better: cheapest first, limited stock splits
    assert prods[2] == ["464322"]              # good: one product with the most stock (kg)
    assert opts[0]["score"]["total"] >= opts[1]["score"]["total"]
    # Cheapest preference: best becomes the cheapest, so better is left out as identical.
    r = c.post("/demo/sandbox/preference", json={"persona": "hotel", "preference": "cheapest"})
    assert r.status_code == 200 and r.json()["card"]["preference"] == "cheapest"
    assert c.post("/demo/sandbox/preference", json={"persona": "hotel", "preference": "x"}
                  ).status_code == 400
    with db.connection() as conn:
        assert db.q(conn, "SELECT COUNT(*) FROM events WHERE type = 'mandate.changed' AND "
                          "text LIKE '%ranking preference%'").scalar_one() == 1
    v = run(sid, "compare_options", "hotel")
    opts = results(v, "request_offer")[0]["detail"]["options"]
    assert [o["label"] for o in opts] == ["best", "good"]
    # 467004's 14 units are still held by the first run's 'better' offer in this sandbox, so
    # the cheapest available coffee now starts with 462245.
    assert opts[0]["offer"]["lines"][0]["product_id"] in ("467004", "462245")
    ids = {o["offer"]["offer_id"] for o in opts}
    assert len(ids) == len(opts)  # separate offers, each signed and holding stock
    with db.connection() as conn:
        n = db.q(conn, "SELECT COUNT(DISTINCT offer_id) FROM stock_holds WHERE offer_id IN "
                       "(" + ",".join(f"'{i}'" for i in ids) + ")").scalar_one()
    assert n == len(ids)
    from handshake import signing

    for o in opts:
        assert signing.verify(o["jws"], signing.jwks(), signing.TYP_OFFER)["offer_id"] ==             o["offer"]["offer_id"]
    # The scripted agent ordered the best option with a rationale: stored, in the event body
    # and on the receipt page as the agent's stated reason.
    order = results(v, "place_order")[0]["detail"]
    with db.connection() as conn:
        rat = db.q(conn, "SELECT rationale_untrusted FROM orders WHERE order_id = :o",
                   o=order["order_id"]).scalar_one()
        body = db.q(conn, "SELECT body_json FROM events WHERE type = 'order.submitted' AND "
                          "trace_id = :t", t=order["trace_id"]).scalar_one()
    assert rat.startswith("Chose the 'best' option")
    assert json.loads(body)["rationale_untrusted"] == rat
    pt = sandbox.party(sid, "hotel")
    rec = c.get(f"/v1/orders/{order['order_id']}/receipt",
                headers={"Authorization": f"Bearer {sandbox.agent_key(pt['agent_id'])}"}).json()
    page = c.get(urlparse(rec["human_url"]).path + "?" + urlparse(rec["human_url"]).query)
    assert "stated reason for its choice (not verified)" in page.text and "Chose the" in page.text


def test_single_option_requests_are_unchanged(world):
    c, sid, d = world
    pt = sandbox.party(sid, "hotel")
    h = {"Authorization": f"Bearer {sandbox.agent_key(pt['agent_id'])}"}
    r = c.post("/v1/offers", headers=h, json={"delivery_date": d.isoformat(), "lines": [
        {"product_ids": ["464398", "464306"], "quantity": {"value": 100, "unit": "L"}}]}).json()
    assert len(r["options"]) == 1 and "score" not in r["options"][0]
    assert r["options"][0]["offer"]["lines"][0]["product_id"] == "464398"  # listed order
    long = "x" * 501
    oid = r["options"][0]["offer"]["offer_id"]
    bad = c.post("/v1/orders", headers={**h, "Idempotency-Key": "rationale-too-long"},
                 json={"offer_id": oid, "rationale": long})
    assert bad.status_code == 400
