"""Dry-run, orders and order status (document 03, section 6; document 09, API-08 to API-15)."""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from handshake import db, events, mandates, orders, timeutil, upstream

from .factories import make_party, mandate_body
from .fakes import FakeUpstream, make_facts

pytestmark = [pytest.mark.db]

PID = "464306"


def delivery():
    d = timeutil.today_helsinki() + timedelta(days=2)
    while d.isoweekday() == 7:  # base mandate allows weekdays 1 to 6 (R9)
        d += timedelta(days=1)
    return d


@pytest.fixture
def client():
    from handshake.api import create_app

    upstream.set_adapter(FakeUpstream([make_facts(
        PID, price="1.01", comparison="1.01", amount=None, available_date=delivery(),
        closing=timeutil.now() + timedelta(days=1))]))
    yield TestClient(create_app())
    upstream.set_adapter(None)


def new_offer(client, party, value=1000) -> dict:
    r = client.post("/v1/offers", headers=party.headers, json={
        "delivery_date": delivery().isoformat(),
        "lines": [{"product_ids": [PID], "quantity": {"value": value, "unit": "L"}}]})
    assert r.status_code == 201, r.json()
    return r.json()["options"][0]["offer"] | {"trace_id": r.json()["trace_id"]}


def order(client, party, offer_id, key="key-0001", **extra):
    headers = dict(party.headers)
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post("/v1/orders", headers=headers, json={"offer_id": offer_id, **extra})


def scalar(sql, **kw):
    with db.connection() as conn:
        return db.q(conn, sql, **kw).scalar_one()


def holds(offer_id):
    with db.connection() as conn:
        return [tuple(r) for r in db.q(
            conn, "SELECT status, expires_at FROM stock_holds WHERE offer_id = :o ORDER BY "
                  "line_no", o=offer_id)]


def types(trace_id, audience=None):
    with db.connection() as conn:
        return [e["type"] for e in events.list_events(conn, trace_id, audience)]


# ---------------------------------------------------------------- dry-run


def test_api08_dry_run_twice_same_result_offer_active(client, catalog, clean_db):
    p = make_party()
    off = new_offer(client, p)
    r1 = client.post("/v1/orders/dry-run", headers=p.headers, json={"offer_id": off["offer_id"]})
    r2 = client.post("/v1/orders/dry-run", headers=p.headers, json={"offer_id": off["offer_id"]})
    assert r1.status_code == r2.status_code == 200, r1.json()
    a, b = r1.json(), r2.json()
    assert a["outcome"] == b["outcome"] == "needs_approval"
    assert a["reasons"] == b["reasons"] and a["reasons"][0]["code"] == "spend_cap_exceeded"
    assert a["mandate_version"] == 1 and a["approver"] == {"name": "Maria Virtanen"}
    assert a["offer_id"] == off["offer_id"] and "evaluated_at" in a
    assert r1.headers["X-Trace-Id"] == off["trace_id"]
    assert scalar("SELECT status FROM offers WHERE offer_id = :o", o=off["offer_id"]) == "active"
    assert scalar("SELECT COUNT(*) FROM orders") == 0
    assert holds(off["offer_id"])[0][0] == "held"
    assert types(off["trace_id"]).count("order.dry_run") == 2


def test_dry_run_errors(client, catalog, clean_db):
    a, b = make_party(), make_party("Other Oy")
    off = new_offer(client, a)
    r = client.post("/v1/orders/dry-run", headers=b.headers, json={"offer_id": off["offer_id"]})
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    r = client.post("/v1/orders/dry-run", headers=a.headers,
                    json={"offer_id": "off_aaaaaaaaaaaaaaaaaaaa"})
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    timeutil.shift_clock(timedelta(minutes=16))
    r = client.post("/v1/orders/dry-run", headers=a.headers, json={"offer_id": off["offer_id"]})
    assert r.status_code == 409 and r.json()["code"] == "offer_expired"
    assert scalar("SELECT status FROM offers WHERE offer_id = :o", o=off["offer_id"]) == "expired"


# ---------------------------------------------------------------- orders


def test_api09_under_cap_confirmed_holds_committed(client, catalog, clean_db):
    p = make_party()
    off = new_offer(client, p, value=500)
    r = order(client, p, off["offer_id"], reference="PO-1", note="Monday breakfast")
    assert r.status_code == 201, r.json()
    o = r.json()
    assert o["status"] == "confirmed" and o["outcome"] == "accepted" and o["reasons"] == []
    assert "approval" not in o
    assert o["trace_id"] == off["trace_id"] == r.headers["X-Trace-Id"]
    assert o["totals"]["excl_tax"] == "505.00" and o["delivery_date"] == delivery().isoformat()
    assert o["links"] == {"self": f"/v1/orders/{o['order_id']}",
                          "receipt": f"/v1/orders/{o['order_id']}/receipt"}
    assert holds(off["offer_id"]) == [("committed", None)]
    assert scalar("SELECT status FROM offers WHERE offer_id = :o", o=off["offer_id"]) == "consumed"
    assert scalar("SELECT note_untrusted FROM orders") == "Monday breakfast"
    ts = types(off["trace_id"])
    assert ts[-4:] == ["order.submitted", "policy.evaluated", "order.confirmed",
                       "holds.changed"]
    assert "holds.changed" not in types(off["trace_id"], "both")
    with db.connection() as conn:
        assert events.verify_chain(events.list_events(conn, off["trace_id"])) == (True, None)


def test_api10_over_cap_pending_approval_no_token(client, catalog, clean_db):
    p = make_party()
    off = new_offer(client, p)
    r = order(client, p, off["offer_id"])
    assert r.status_code == 202, r.json()
    o = r.json()
    assert o["status"] == "pending_approval" and o["outcome"] == "needs_approval"
    assert o["totals"]["excl_tax"] == "1010.00"
    ap = o["approval"]
    assert ap["status"] == "pending" and ap["approver"] == {"name": "Maria Virtanen"}
    assert set(ap) == {"status", "approver", "requested_at", "expires_at"}
    with db.connection() as conn:
        row = db.q(conn, "SELECT * FROM approvals").mappings().one()
    token = orders.approval_token(row["approval_id"])
    assert row["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    assert len(token) == 43 and row["status"] == "pending"
    assert row["approver_contact"] == "maria@example.com"
    for secret in ("/approve", "http", token, row["token_sha256"], row["approval_id"]):
        assert secret not in r.text
    # Approval window: min(now + TTL, earliest order_by); holds stay held until then.
    exp = timeutil.from_db(row["expires_at"])
    assert exp - timeutil.from_db(row["requested_at"]) <= timedelta(hours=1)
    assert holds(off["offer_id"]) == [("held", row["expires_at"])]
    with db.connection() as conn:
        assert orders.find_approval_by_token(conn, token)["order_id"] == o["order_id"]
        assert orders.find_approval_by_token(conn, "x" * 43) is None
    assert orders.approval_url(row["approval_id"]).endswith("/approve/" + token)
    with db.connection() as conn:
        evs = events.list_events(conn, off["trace_id"])
    assert [e["type"] for e in evs][-4:] == ["order.submitted", "policy.evaluated",
                                             "approval.requested", "holds.changed"]
    pe = next(e for e in evs if e["type"] == "policy.evaluated")
    assert pe["text"] == ("Total 1010.00 EUR exceeded the 1000.00 EUR cap. "
                          "Sent to Maria Virtanen for approval.")
    assert pe["actor_type"] == "gateway" and pe["audience"] == "both"
    assert not any(token in e["text"] for e in evs)


def test_api11_same_key_returns_same_order(client, catalog, clean_db):
    p = make_party()
    off = new_offer(client, p)
    r1 = order(client, p, off["offer_id"], key="same-key-1")
    r2 = order(client, p, off["offer_id"], key="same-key-1")
    assert r1.status_code == r2.status_code == 202
    assert r1.json() == r2.json()
    assert r2.headers["X-Trace-Id"] == off["trace_id"]
    assert scalar("SELECT COUNT(*) FROM orders") == 1
    assert scalar("SELECT COUNT(*) FROM approvals") == 1
    assert types(off["trace_id"]).count("order.submitted") == 1


def test_api12_same_key_different_offer_conflict(client, catalog, clean_db):
    p = make_party()
    off1, off2 = new_offer(client, p), new_offer(client, p, value=10)
    assert order(client, p, off1["offer_id"], key="shared-key").status_code == 202
    r = order(client, p, off2["offer_id"], key="shared-key")
    assert r.status_code == 409 and r.json()["code"] == "idempotency_conflict"
    assert scalar("SELECT status FROM offers WHERE offer_id = :o", o=off2["offer_id"]) == "active"


def test_api13_consumed_offer_new_key(client, catalog, clean_db):
    p = make_party()
    off = new_offer(client, p)
    assert order(client, p, off["offer_id"], key="first-key").status_code == 202
    r = order(client, p, off["offer_id"], key="second-key")
    assert r.status_code == 409 and r.json()["code"] == "offer_consumed"
    assert scalar("SELECT COUNT(*) FROM orders") == 1


def test_api14_expired_offer(client, catalog, clean_db):
    p = make_party()
    off = new_offer(client, p)
    timeutil.shift_clock(timedelta(minutes=16))
    r = order(client, p, off["offer_id"])
    assert r.status_code == 409 and r.json()["code"] == "offer_expired"
    assert scalar("SELECT status FROM offers WHERE offer_id = :o", o=off["offer_id"]) == "expired"
    assert holds(off["offer_id"])[0][0] == "released"
    assert scalar("SELECT COUNT(*) FROM orders") == 0
    assert scalar("SELECT COUNT(*) FROM idempotency_keys") == 0
    assert "offer.expired" in types(off["trace_id"])


def test_api15_other_agents_order_forbidden(client, catalog, clean_db):
    a, b = make_party(), make_party("Other Oy")
    off = new_offer(client, a)
    oid = order(client, a, off["offer_id"]).json()["order_id"]
    r = client.get(f"/v1/orders/{oid}", headers=b.headers)
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    r = order(client, b, off["offer_id"], key="b-key-0001")
    assert r.status_code == 403
    r = client.get("/v1/orders/ord_aaaaaaaaaaaaaaaaaaaa", headers=a.headers)
    assert r.status_code == 404 and r.json()["code"] == "not_found"


@pytest.mark.parametrize("key", [None, "short", "x" * 65])
def test_idempotency_key_required(client, catalog, clean_db, key):
    p = make_party()
    off = new_offer(client, p)
    r = order(client, p, off["offer_id"], key=key)
    assert r.status_code == 400 and r.json()["code"] == "invalid_request"
    assert scalar("SELECT status FROM offers WHERE offer_id = :o", o=off["offer_id"]) == "active"


def test_rejected_outcome_releases_holds(client, catalog, clean_db):
    p = make_party()
    off = new_offer(client, p, value=10)
    # The offer builder already filters on categories, so narrow the mandate after issuing:
    # evaluation uses the mandate current at order time (document 05, section 3).
    with db.transaction() as conn:
        mandates.replace(conn, p.agent_id, p.customer_id,
                         mandate_body(allowed_categories=["10430"]))
    r = order(client, p, off["offer_id"])
    assert r.status_code == 200, r.json()
    assert scalar("SELECT mandate_version FROM orders") == 2
    o = r.json()
    assert o["status"] == "rejected" and o["outcome"] == "rejected" and "approval" not in o
    assert o["reasons"][0]["code"] == "category_not_allowed"
    assert holds(off["offer_id"])[0][0] == "released"
    assert scalar("SELECT status FROM offers WHERE offer_id = :o", o=off["offer_id"]) == "consumed"
    assert types(off["trace_id"])[-4:] == ["order.submitted", "policy.evaluated",
                                           "order.closed", "holds.changed"]
    g = client.get(f"/v1/orders/{o['order_id']}", headers=p.headers)
    assert g.status_code == 200 and g.json()["status"] == "rejected"


def test_get_order_current_status_and_approval_expiry(client, catalog, clean_db):
    p = make_party()
    off = new_offer(client, p)
    created = order(client, p, off["offer_id"]).json()
    g = client.get(f"/v1/orders/{created['order_id']}", headers=p.headers)
    assert g.status_code == 200 and g.headers["X-Trace-Id"] == off["trace_id"]
    assert g.json() == created
    # Phase 3 helper: lazy approval expiry (document 05, section 6).
    timeutil.shift_clock(timedelta(hours=2))
    g = client.get(f"/v1/orders/{created['order_id']}", headers=p.headers).json()
    assert g["status"] == "expired" and g["approval"]["status"] == "expired"
    assert "decided_at" in g["approval"]
    assert holds(off["offer_id"])[0][0] == "released"
    assert types(off["trace_id"])[-3:] == ["approval.expired", "order.closed", "holds.changed"]
    # Idempotent: a second read changes nothing.
    n = len(types(off["trace_id"]))
    client.get(f"/v1/orders/{created['order_id']}", headers=p.headers)
    assert len(types(off["trace_id"])) == n


def test_phase2_done_when_high_cap_accepted_low_cap_needs_approval(client, catalog, clean_db):
    high = make_party(limits={"max_order_total_excl_tax": "5000.00"})
    low = make_party("Low cap Oy")
    r_high = order(client, high, new_offer(client, high)["offer_id"])
    r_low = order(client, low, new_offer(client, low)["offer_id"])
    assert (r_high.status_code, r_high.json()["outcome"]) == (201, "accepted")
    assert (r_low.status_code, r_low.json()["outcome"]) == (202, "needs_approval")
    again = order(client, low, r_low.json()["offer_id"])
    assert again.json()["order_id"] == r_low.json()["order_id"]


def test_integrity_race_returns_stored_result(client, catalog, clean_db, monkeypatch):
    """A concurrent winner on the same key: the loser replays the stored response."""
    p = make_party()
    off = new_offer(client, p)
    first = order(client, p, off["offer_id"], key="race-key-1").json()
    real = orders._stored
    calls = {"n": 0}

    def miss_once(conn, agent_id, key, **kw):  # the pre-checks miss the winner's row
        calls["n"] += 1
        return None if calls["n"] <= 2 else real(conn, agent_id, key, **kw)

    monkeypatch.setattr(orders, "_stored", miss_once)
    monkeypatch.setattr(orders, "_check_offer", lambda *a: None)
    with db.transaction() as conn:  # make the offer look unused again; orders.offer_id clashes
        db.q(conn, "UPDATE offers SET status = 'active' WHERE offer_id = :o", o=off["offer_id"])
    r = order(client, p, off["offer_id"], key="race-key-1")
    assert r.status_code == 202 and r.json()["order_id"] == first["order_id"]
    assert scalar("SELECT COUNT(*) FROM orders") == 1


def test_overlapping_same_key_request_replays_instead_of_offer_consumed(
        client, catalog, clean_db, monkeypatch):
    """Code review Important #1: a same-key request whose first idempotency lookup ran before
    the original committed must get the stored result, not 409 offer_consumed."""
    p = make_party()
    off = new_offer(client, p)
    first = order(client, p, off["offer_id"], key="overlap-key-1")
    assert first.status_code == 202
    real = orders._stored
    calls = {"n": 0}

    def stale_first_lookup(conn, agent_id, key, **kw):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(conn, agent_id, key, **kw)

    monkeypatch.setattr(orders, "_stored", stale_first_lookup)
    r = order(client, p, off["offer_id"], key="overlap-key-1")
    assert r.status_code == 202, r.text
    assert r.json()["order_id"] == first.json()["order_id"]
    assert scalar("SELECT COUNT(*) FROM orders") == 1
    # A different key on the consumed offer is still refused.
    monkeypatch.setattr(orders, "_stored", real)
    assert order(client, p, off["offer_id"], key="overlap-key-2").json()["code"] == "offer_consumed"


def test_r7_weekly_cap_counts_this_weeks_orders(client, catalog, clean_db):
    """R7 end to end: the second 1,000 L order of the week exceeds a 1500.00 weekly cap."""
    p = make_party(limits={"max_order_total_excl_tax": "5000.00",
                           "max_weekly_total_excl_tax": "1500.00"})
    first = order(client, p, new_offer(client, p)["offer_id"], key="week-key-1")
    assert first.status_code == 201 and first.json()["outcome"] == "accepted"
    off2 = new_offer(client, p)
    dry = client.post("/v1/orders/dry-run", headers=p.headers,
                      json={"offer_id": off2["offer_id"]}).json()
    assert dry["outcome"] == "needs_approval"
    assert [r["code"] for r in dry["reasons"]] == ["period_cap_exceeded"]
    assert dry["reasons"][0]["details"]["week_total_excl_tax"] == "1010.00"
    assert dry["reasons"][0]["details"]["projected_total_excl_tax"] == "2020.00"
    second = order(client, p, off2["offer_id"], key="week-key-2")
    assert second.status_code == 202 and second.json()["status"] == "pending_approval"
