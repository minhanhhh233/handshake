"""Signed receipt endpoint (task T-402; document 03, section 6; document 05, 7.4; S-05)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from handshake import db, events, mandates, orders, signing, timeutil, upstream
from handshake.config import get_settings

from .factories import make_party, mandate_body
from .fakes import FakeUpstream, make_facts

pytestmark = [pytest.mark.db]

PID = "464306"


def delivery():
    d = timeutil.today_helsinki() + timedelta(days=2)
    while d.isoweekday() == 7:
        d += timedelta(days=1)
    return d


@pytest.fixture
def client():
    from handshake.api import create_app

    if not get_settings().admin_key:
        pytest.skip("ADMIN_KEY not set")
    upstream.set_adapter(FakeUpstream([make_facts(
        PID, price="1.01", comparison="1.01", amount=None, available_date=delivery(),
        closing=timeutil.now() + timedelta(days=1))]))
    yield TestClient(create_app())
    upstream.set_adapter(None)


def place(client, party, value=1000, key="key-receipt-1", before_order=None) -> dict:
    r = client.post("/v1/offers", headers=party.headers, json={
        "delivery_date": delivery().isoformat(),
        "lines": [{"product_ids": [PID], "quantity": {"value": value, "unit": "L"}}]})
    assert r.status_code == 201, r.json()
    offer_id = r.json()["options"][0]["offer"]["offer_id"]
    if before_order:
        before_order()
    r = client.post("/v1/orders", headers=party.headers | {"Idempotency-Key": key},
                    json={"offer_id": offer_id, "reference": "PO-7", "note": "<b>hi</b>"})
    assert r.status_code in (200, 201, 202), r.json()
    return r.json()


def decide(order_id: str, decision: str) -> None:
    with db.connection() as conn:
        a = orders.load_approval(conn, order_id)
    res = orders.decide_approval(orders.approval_token(a["approval_id"]), decision, None)
    assert res["result"] in ("approved", "declined")


def all_events(order_id: str) -> list[dict]:
    with db.connection() as conn:
        o = orders.load_order(conn, order_id)
        return events.list_events(conn, o["trace_id"])


def receipt_token(order_id: str) -> str:
    with db.connection() as conn:
        return orders.load_order(conn, order_id)["receipt_token"]


def test_receipt_shape_signature_and_chain(client, catalog, clean_db):
    p = make_party()
    o = place(client, p)
    assert o["status"] == "pending_approval"
    decide(o["order_id"], "approve")

    r = client.get(f"/v1/orders/{o['order_id']}/receipt", headers=p.headers)
    assert r.status_code == 200, r.json()
    assert r.headers["x-trace-id"] == o["trace_id"]
    body = r.json()
    assert set(body) == {"receipt", "jws", "human_url"}
    rec = body["receipt"]
    assert set(rec) == {"order_id", "trace_id", "status", "generated_at", "summary", "events",
                        "chain", "offer_jws"}
    assert rec["order_id"] == o["order_id"] and rec["trace_id"] == o["trace_id"]
    assert rec["status"] == "confirmed"
    assert rec["generated_at"].endswith(("+02:00", "+03:00"))
    d = delivery()
    month = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
             "Dec"][d.month - 1]
    assert rec["summary"].startswith("Ordered 1,000 L of ")
    assert rec["summary"].endswith(
        f"for delivery on {d.day} {month} {d.year}. Total 1010.00 EUR excl. tax. "
        "Approved by Maria Virtanen.")
    for e in rec["events"]:
        assert set(e) == {"seq", "event_id", "type", "at", "actor", "text", "hash"}
        assert set(e["actor"]) <= {"type", "id"}
    approver = next(e for e in rec["events"] if e["type"] == "approval.decided")
    assert approver["actor"] == {"type": "approver", "id": "Maria Virtanen"}
    gateway = next(e for e in rec["events"] if e["type"] == "policy.evaluated")
    assert gateway["actor"] == {"type": "gateway"}

    # the offer JWS is the original
    off = client.get(f"/v1/offers/{o['offer_id']}", headers=p.headers).json()
    assert rec["offer_jws"] == off["jws"]

    # the receipt JWS verifies and its payload is the receipt
    payload = signing.verify(body["jws"], signing.jwks(), signing.TYP_RECEIPT)
    assert payload == rec
    with pytest.raises(signing.SignatureError):
        signing.verify(body["jws"], signing.jwks(), signing.TYP_OFFER)

    # S-05: head is the hash of the last event in the trace, length counts all events
    evs = all_events(o["order_id"])
    assert evs[-1]["audience"] == "aimo_only"  # holds.changed comes last
    assert rec["chain"] == {"algorithm": "sha256", "head": evs[-1]["hash"],
                            "length": len(evs)}
    assert events.verify_chain(evs) == (True, None)

    # buyer events: audience both only, in order, with their stored hashes
    both = [e for e in evs if e["audience"] == "both"]
    assert [e["event_id"] for e in rec["events"]] == [e["event_id"] for e in both]
    assert [e["hash"] for e in rec["events"]] == [e["hash"] for e in both]
    types = {e["type"] for e in rec["events"]}
    assert not types & {"upstream.queried", "holds.changed", "search.performed",
                        "product.read"}
    assert len(rec["events"]) < rec["chain"]["length"]

    # human url
    url = body["human_url"]
    assert url.startswith(get_settings().public_base_url + "/receipts/" + o["order_id"])
    assert url.endswith("?t=" + receipt_token(o["order_id"]))
    # untrusted text stays out of the signed receipt
    assert "<b>hi</b>" not in str(rec)


def test_receipt_auth(client, catalog, clean_db):
    p = make_party()
    o = place(client, p)
    other = make_party("Other Hotels")
    r = client.get(f"/v1/orders/{o['order_id']}/receipt", headers=other.headers)
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    r = client.get("/v1/orders/ord_aaaaaaaaaaaaaaaaaaaa/receipt", headers=p.headers)
    assert r.status_code == 404
    assert client.get(f"/v1/orders/{o['order_id']}/receipt").status_code == 401


def test_receipt_accepted_order(client, catalog, clean_db):
    p = make_party()
    o = place(client, p, value=500)
    assert o["status"] == "confirmed"
    rec = client.get(f"/v1/orders/{o['order_id']}/receipt", headers=p.headers).json()["receipt"]
    assert rec["status"] == "confirmed"
    assert rec["summary"].endswith("Within the mandate, so no approval was needed.")
    assert rec["chain"]["head"] == all_events(o["order_id"])[-1]["hash"]


def test_receipt_declined_order(client, catalog, clean_db):
    p = make_party()
    o = place(client, p)
    decide(o["order_id"], "decline")
    rec = client.get(f"/v1/orders/{o['order_id']}/receipt", headers=p.headers).json()["receipt"]
    assert rec["status"] == "declined"
    assert "was declined by Maria Virtanen" in rec["summary"]
    assert rec["events"][-1]["type"] == "order.closed"
    evs = all_events(o["order_id"])
    assert rec["chain"] == {"algorithm": "sha256", "head": evs[-1]["hash"], "length": len(evs)}


def test_receipt_rejected_order(client, catalog, clean_db):
    p = make_party()

    def narrow():  # the offer builder filters categories, so narrow the mandate afterwards
        with db.transaction() as conn:
            mandates.replace(conn, p.agent_id, p.customer_id,
                             mandate_body(allowed_categories=["10430"]))

    o = place(client, p, value=10, before_order=narrow)
    assert o["status"] == "rejected", o
    r = client.get(f"/v1/orders/{o['order_id']}/receipt", headers=p.headers)
    assert r.status_code == 200
    rec = r.json()["receipt"]
    assert rec["status"] == "rejected"
    assert "was rejected by the mandate" in rec["summary"]
    signing.verify(r.json()["jws"], signing.jwks(), signing.TYP_RECEIPT)


def test_receipt_applies_due_expiry(client, catalog, clean_db):
    p = make_party()
    o = place(client, p)
    timeutil.shift_clock(timedelta(seconds=get_settings().approval_ttl_seconds + 60))
    rec = client.get(f"/v1/orders/{o['order_id']}/receipt", headers=p.headers).json()["receipt"]
    assert rec["status"] == "expired"
    assert "did not decide in time" in rec["summary"]
