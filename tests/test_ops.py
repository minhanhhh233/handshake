"""Valio Aimo operations role, scoped to the visitor's sandbox (stage 4; document 09, 6g)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from handshake import db, live_demo, scenarios

from .test_scenarios import world  # noqa: F401  (fixture re-export)

pytestmark = pytest.mark.db


def run(sid, scenario, persona, ip):
    trace = scenarios.start(scenario, persona, sid, ip, wait=True)
    return live_demo.view(trace, sandbox_id=sid)


def other_client(c):
    o = TestClient(c.app, follow_redirects=False)
    o.get("/demo/sandbox?persona=hotel", headers={"X-Forwarded-For": "198.51.100.99"})
    return o


def test_ops_view_is_sandbox_scoped(world):  # noqa: F811
    c, sid, _ = world
    run(sid, "hotel_milk", "hotel", "198.51.100.81")
    v = c.get("/demo/ops").json()
    assert [a["persona"] for a in v["agents"]] == ["hotel"]
    types = {a["type"] for a in v["access"]}
    assert {"search.performed", "offer.requested"} <= types  # fake upstream logs no query
    assert v["offers"] and v["orders"][0]["status"] == "pending_approval"
    assert v["orders"][0]["receipt_path"].startswith("/demo/ops/receipts/ord_")
    o = other_client(c)
    v2 = o.get("/demo/ops").json()
    # The other sandbox sees only its own (fresh) hotel agent, none of this sandbox's records.
    assert {a["agent_id"] for a in v2["agents"]}.isdisjoint({a["agent_id"] for a in v["agents"]})
    assert v2["access"] == [] and v2["offers"] == [] and v2["orders"] == []
    assert TestClient(c.app).get("/demo/ops").status_code == 409
    s = c.get("/demo/sandbox?persona=operations").json()
    assert s["card"]["persona"] == "operations" and s["ops"]["orders"]


def test_ops_receipt_suspend_and_tamper(world):  # noqa: F811
    c, sid, _ = world
    run(sid, "hotel_milk", "hotel", "198.51.100.82")
    v = c.get("/demo/ops").json()
    order = v["orders"][0]
    page = c.get(order["receipt_path"])
    assert page.status_code == 200 and "Valio Aimo view" in page.text
    assert "Record intact" in page.text
    o = other_client(c)
    assert o.get(order["receipt_path"]).status_code == 404
    assert TestClient(c.app).get(order["receipt_path"]).status_code == 404
    # Another sandbox can neither tamper with nor suspend this sandbox's records.
    assert o.post("/demo/ops/tamper", json={"order_id": order["order_id"]}).status_code == 404
    agent = v["agents"][0]
    assert o.post("/demo/ops/suspend", json={"agent_id": agent["agent_id"],
                                             "suspended": True}).status_code == 404
    # Tamper: the receipt reports the record as broken.
    r = c.post("/demo/ops/tamper", json={"order_id": order["order_id"]})
    assert r.status_code == 200 and r.json()["changed"]["outcome"]["to"] == "accepted"
    page = c.get(order["receipt_path"]).text
    assert "Record intact" not in page
    # Suspend: the agent is refused (R1), and the change is an admin event.
    r = c.post("/demo/ops/suspend", json={"agent_id": agent["agent_id"], "suspended": True})
    assert r.status_code == 200 and r.json()["agents"][0]["status"] == "suspended"
    v = run(sid, "suspended_probe", "hotel", "198.51.100.83")
    res = {s["title"]: s for s in v["steps"] if s["kind"] == "result"}
    for t in ("get_mandate", "request_offer"):
        assert res[t]["detail"]["status"] == 403 and res[t]["detail"]["code"] == "agent_suspended"
    assert res["search_products"]["status"] == "ok"  # public search still works
    assert "place_order" not in res
    with db.connection() as conn:
        assert db.q(conn, "SELECT COUNT(*) FROM events WHERE type = 'agent.status_changed'"
                    ).scalar_one() == 1
    r = c.post("/demo/ops/suspend", json={"agent_id": agent["agent_id"], "suspended": False})
    assert r.json()["agents"][0]["status"] == "active"
    assert c.post("/demo/ops/suspend", json={"agent_id": 5}).status_code == 400
    assert "suspended_probe" not in [x["id"] for x in c.get(
        "/demo/sandbox?persona=hotel").json()["scenarios"]["scripted"]]


def test_suspended_agent_refused_on_api_and_r1_remains(world):  # noqa: F811
    from handshake import sandbox

    c, sid, d = world
    pt = sandbox.party(sid, "hotel")
    h = {"Authorization": f"Bearer {sandbox.agent_key(pt['agent_id'])}"}
    o = c.post("/v1/offers", headers=h, json={"delivery_date": d.isoformat(), "lines": [
        {"product_ids": ["464306"], "quantity": {"value": 10, "unit": "L"}}]}).json()
    oid = o["options"][0]["offer"]["offer_id"]
    with db.transaction() as conn:
        db.q(conn, "UPDATE agents SET status = 'suspended' WHERE agent_id = :a",
             a=pt["agent_id"])
    for method, path, body in (("GET", "/v1/mandates/current", None),
                               ("POST", "/v1/offers", {"delivery_date": d.isoformat(), "lines": [
                                   {"product_ids": ["464306"],
                                    "quantity": {"value": 10, "unit": "L"}}]}),
                               ("POST", "/v1/orders/dry-run", {"offer_id": oid}),
                               ("POST", "/v1/orders", {"offer_id": oid})):
        r = c.request(method, path, headers={**h, "Idempotency-Key": "suspended-1234"},
                      json=body)
        assert r.status_code == 403 and r.json()["code"] == "agent_suspended", (path, r.text)
    assert c.get("/v1/products?category=10400&limit=2", headers=h).status_code == 200
    # R1 stays as the defensive check inside order evaluation.
    from handshake import orders
    from handshake.models import OrderRequest

    _, out = orders.place_order(OrderRequest(offer_id=oid), agent_id=pt["agent_id"],
                                customer_id=pt["customer_id"], agent_status="suspended",
                                idem_key="r1-defensive-check")
    assert out["status"] == "rejected" and "mandate_inactive" in [
        x["code"] for x in out["reasons"]]
