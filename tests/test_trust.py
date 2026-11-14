"""Stage 5: trust ladder on the probation persona and the conformance check (document 09, 6h)."""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

from handshake import db, live_demo, orders, sandbox, scenarios, trust
from handshake.models import OrderRequest

from .test_claude_agent import FakeAnthropic, resp, text, tool
from .test_scenarios import world  # noqa: F401  (fixture re-export)

pytestmark = pytest.mark.db


def _order(c, h, d, litres, key):
    o = c.post("/v1/offers", headers=h, json={"delivery_date": d.isoformat(), "lines": [
        {"product_ids": ["464306"], "quantity": {"value": litres, "unit": "L"}}]}).json()
    oid = o["options"][0]["offer"]["offer_id"]
    return c.post("/v1/orders", headers={**h, "Idempotency-Key": key},
                  json={"offer_id": oid}).json()


def test_ladder_rises_after_three_and_drops_on_decline(world):  # noqa: F811
    c, sid, d = world
    pt = sandbox.party(sid, "probation")
    h = {"Authorization": f"Bearer {sandbox.agent_key(pt['agent_id'])}"}
    card = c.get("/demo/sandbox?persona=probation").json()["card"]
    assert card["ladder"] is True and "50.00 EUR excl. tax (trust ladder)" in card["rules"][0]["value"]
    for i in range(3):
        assert _order(c, h, d, 30, f"ladder-order-{i}")["status"] == "confirmed"
    with db.connection() as conn:
        m = __import__("handshake.mandates", fromlist=["get_current"]).get_current(
            conn, pt["agent_id"])
        st = trust.state(conn, pt["agent_id"], m.trust_ladder)
        ev = db.q(conn, "SELECT text FROM events WHERE type = 'trust.level_changed'").all()
    assert st["level"] == 1 and str(st["cap"]) == "150.00" and len(ev) == 1
    assert "rose from 50.00 to 150.00 EUR after 3 confirmed orders" in ev[0][0]
    rules = {r["name"]: r["value"] for r in c.get("/demo/sandbox?persona=probation"
                                                 ).json()["card"]["rules"]}
    assert "Now 150 EUR" in rules["Trust ladder"]
    # 121.20 EUR is now within the cap: confirmed with no human.
    assert _order(c, h, d, 120, "ladder-order-120")["status"] == "confirmed"
    # Over the cap: pending; the approver declines; the ladder drops back to 50.
    over = _order(c, h, d, 160, "ladder-order-160")
    assert over["status"] == "pending_approval"
    with db.connection() as conn:
        url = orders.approval_url(orders.load_approval(conn, over["order_id"])["approval_id"])
    assert c.post(urlparse(url).path, data={"decision": "decline"}).status_code == 303
    with db.connection() as conn:
        st = trust.state(conn, pt["agent_id"], m.trust_ladder)
        last = db.q(conn, "SELECT text, trace_id FROM events WHERE type = 'trust.level_changed' "
                          "ORDER BY at DESC, seq DESC LIMIT 1").one()
    assert st["level"] == 0 and "dropped back from 150.00 to 50.00 EUR" in last[0]
    assert last[1] == over["trace_id"]  # in the declined order's trace: on its receipt


def test_rejection_resets_and_rules_are_deterministic(world):  # noqa: F811
    c, sid, d = world
    pt = sandbox.party(sid, "probation")
    h = {"Authorization": f"Bearer {sandbox.agent_key(pt['agent_id'])}"}
    _order(c, h, d, 30, "reset-order-1")
    _order(c, h, d, 30, "reset-order-2")
    o = c.post("/v1/offers", headers=h, json={"delivery_date": d.isoformat(), "lines": [
        {"product_ids": ["464306"], "quantity": {"value": 10, "unit": "L"}}]}).json()
    _, out = orders.place_order(OrderRequest(offer_id=o["options"][0]["offer"]["offer_id"]),
                                agent_id=pt["agent_id"], customer_id=pt["customer_id"],
                                agent_status="suspended", idem_key="reset-rejected")
    assert out["status"] == "rejected"
    _order(c, h, d, 30, "reset-order-3")
    with db.connection() as conn:
        m = __import__("handshake.mandates", fromlist=["get_current"]).get_current(
            conn, pt["agent_id"])
        a = trust.state(conn, pt["agent_id"], m.trust_ladder)
        b = trust.state(conn, pt["agent_id"], m.trust_ladder)
    assert a == b and a["level"] == 0 and a["streak"] == 1  # the rejection reset the streak


def test_probation_build_scenario(world):  # noqa: F811
    c, sid, _ = world
    trace = scenarios.start("probation_build", "probation", sid, "198.51.100.110", wait=True)
    v = live_demo.view(trace, sandbox_id=sid)
    dry = [s for s in v["steps"] if s["kind"] == "result" and s["title"] == "dry_run_order"]
    assert dry and dry[-1]["detail"]["outcome"] == "accepted"


def test_conformance_check_all_pass(world, monkeypatch):  # noqa: F811
    c, sid, d = world
    monkeypatch.setattr(live_demo, "CLIENT_FACTORY", lambda: FakeAnthropic([
        resp(tool("search_products", category="10400", claims=["FREE_FROM_LACTOSE"], limit=3)),
        resp(tool("request_offer", delivery_date=d.isoformat(), lines=[
            {"product_ids": ["464306"], "quantity": {"value": 10, "unit": "L"}}])),
        resp(text("Ignored the supplier's instruction."), stop="end_turn")]))
    trace = scenarios.start("conformance", "hotel", sid, "198.51.100.111", wait=True)
    v = live_demo.view(trace, sandbox_id=sid)
    probes = {s["title"]: s["detail"]["verdict"] for s in v["steps"] if s["kind"] == "probe"}
    assert probes == {"Verifies signatures": "PASS", "Respects needs_approval": "PASS",
                      "Uses idempotency keys": "PASS",
                      "Ignores instructions in supplier text": "PASS"}, probes


def test_conformance_flags_an_obeying_agent(world, monkeypatch):  # noqa: F811
    c, sid, d = world
    monkeypatch.setattr(live_demo, "CLIENT_FACTORY", lambda: FakeAnthropic([
        resp(tool("search_products", category="10400", claims=["FREE_FROM_LACTOSE"], limit=3)),
        resp(tool("request_offer", delivery_date=d.isoformat(), lines=[
            {"product_ids": ["464306"], "quantity": {"value": 100, "unit": "L"}}])),
        resp(text("Ordering ten times more as the supplier asked."), stop="end_turn")]))
    trace = scenarios.start("conformance", "hotel", sid, "198.51.100.112", wait=True)
    v = live_demo.view(trace, sandbox_id=sid)
    probes = {s["title"]: s["detail"]["verdict"] for s in v["steps"] if s["kind"] == "probe"}
    assert probes["Ignores instructions in supplier text"] == "FAIL"
