"""Approval pages, approval decisions, approval-link, reset and console (T-301 to T-303;
document 03, sections 7 and 8; document 09, API-16 to API-19)."""

from __future__ import annotations

import importlib.util
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from handshake import db, events, orders, timeutil, upstream
from handshake.config import get_settings

from .factories import make_party
from .fakes import FakeUpstream, make_facts

pytestmark = [pytest.mark.db]

PID = "464306"
NOTE = "<script>alert(1)</script> Monday breakfast"
_parties: dict[str, dict] = {}


def delivery():
    d = timeutil.today_helsinki() + timedelta(days=2)
    while d.isoweekday() == 7:
        d += timedelta(days=1)
    return d


@pytest.fixture
def admin():
    key = get_settings().admin_key
    if not key:
        pytest.skip("ADMIN_KEY not set")
    return {"X-Admin-Key": key}


@pytest.fixture
def client(admin):
    from handshake.api import create_app

    upstream.set_adapter(FakeUpstream([make_facts(
        PID, price="1.01", comparison="1.01", amount=None, available_date=delivery(),
        closing=timeutil.now() + timedelta(days=1))]))
    yield TestClient(create_app())
    upstream.set_adapter(None)


@pytest.fixture
def demo_mode(monkeypatch):
    """Settings with DEMO_MODE=true for the duration of the test."""
    monkeypatch.setenv("DEMO_MODE", "true")
    get_settings.cache_clear()
    assert get_settings().demo_mode
    yield
    get_settings.cache_clear()


@pytest.fixture
def no_demo_mode(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def pending_order(client, party, note=NOTE) -> dict:
    r = client.post("/v1/offers", headers=party.headers, json={
        "delivery_date": delivery().isoformat(),
        "lines": [{"product_ids": [PID], "quantity": {"value": 1000, "unit": "L"}}]})
    assert r.status_code == 201, r.json()
    offer = r.json()["options"][0]["offer"]
    body = {"offer_id": offer["offer_id"], "reference": "PO-7"}
    if note:
        body["note"] = note
    r = client.post("/v1/orders", headers=party.headers | {"Idempotency-Key": "key-pages-1"},
                    json=body)
    assert r.status_code == 202, r.json()
    _parties[r.json()["order_id"]] = party.headers
    return r.json()


def token_of(order_id: str) -> str:
    with db.connection() as conn:
        a = orders.load_approval(conn, order_id)
    return orders.approval_token(a["approval_id"])


def state(order_id: str) -> dict:
    with db.connection() as conn:
        o = orders.load_order(conn, order_id)
        a = orders.load_approval(conn, order_id)
        h = [r[0] for r in db.q(conn, "SELECT status FROM stock_holds WHERE offer_id = :o",
                                o=o["offer_id"])]
        evs = events.list_events(conn, o["trace_id"])
    return {"order": o["status"], "approval": a["status"], "holds": h, "events": evs,
            "decided_at": a["decided_at"], "comment": a["comment_untrusted"]}


def decide(client, token, decision, comment=None):
    data = {"decision": decision}
    if comment is not None:
        data["comment"] = comment
    return client.post(f"/approve/{token}", data=data, follow_redirects=False)


# ---------------------------------------------------------------- page


def test_approval_page_content_and_headers(client, catalog, clean_db):
    o = pending_order(client, make_party())
    token = token_of(o["order_id"])
    r = client.get(f"/approve/{token}")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-frame-options"] == "DENY"
    html = r.text
    # cap meter sentence and the reasons
    text = re.sub(r"<[^>]+>", "", html).replace("&nbsp;", " ")
    assert "1010.00 EUR is 10.00 over your 1000.00 EUR limit." in text
    assert 'class="bar"' in html
    # order facts
    assert "Nordlys Hotels" in html and "Nordlys Hotels purchasing agent" in html
    assert "1,000 units, 1,000 L" in html and "1146.35" in html
    assert o["offer_id"] in html and "Approve order" in html and "Decline order" in html
    # untrusted note escaped and labelled
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Note from the agent (not verified)" in html
    # the token is not rendered anywhere (the form posts to its own URL)
    assert token not in html
    assert "Valio Aimo confirms stock" in html


def test_order_response_has_no_token_or_url(client, catalog, clean_db):
    o = pending_order(client, make_party())
    token = token_of(o["order_id"])
    raw = str(o)
    assert token not in raw and "/approve/" not in raw
    assert set(o["approval"]) <= {"status", "approver", "requested_at", "expires_at"}


def test_unknown_token_404_page(client, catalog, clean_db):
    r = client.get("/approve/" + "x" * 43)
    assert r.status_code == 404 and "text/html" in r.headers["content-type"]
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "not valid" in r.text
    r = client.post("/approve/" + "x" * 43, data={"decision": "approve"}, follow_redirects=False)
    assert r.status_code == 404


# ---------------------------------------------------------------- decisions


def test_api16_approve_confirms_and_commits(client, catalog, clean_db):
    o = pending_order(client, make_party())
    token = token_of(o["order_id"])
    r = decide(client, token, "approve", "  Fine for Monday <b>ok</b>  ")
    assert r.status_code == 303 and r.headers["location"] == f"/approve/{token}/done"
    assert r.headers["referrer-policy"] == "no-referrer"
    s = state(o["order_id"])
    assert s["order"] == "confirmed" and s["approval"] == "approved"
    assert s["holds"] == ["committed"] and s["decided_at"] is not None
    assert s["comment"] == "Fine for Monday <b>ok</b>"
    tail = [(e["type"], e["actor_type"], e["actor_id"], e["audience"]) for e in s["events"][-3:]]
    assert tail == [("approval.decided", "approver", "Maria Virtanen", "both"),
                    ("order.confirmed", "gateway", None, "both"),
                    ("holds.changed", "gateway", None, "aimo_only")]
    assert s["events"][-3]["text"] == "Maria Virtanen approved the order."
    assert events.verify_chain(s["events"])[0]

    done = client.get(r.headers["location"])
    assert done.status_code == 200 and done.headers["cache-control"] == "no-store"
    assert "Approved. Your agent will see the order as confirmed." in done.text
    assert "&lt;b&gt;ok&lt;/b&gt;" in done.text and "<b>ok</b>" not in done.text
    # refresh-safe
    assert "Approved." in client.get(r.headers["location"]).text

    # the agent sees it
    p = client.get(f"/v1/orders/{o['order_id']}", headers=make_headers(o))
    assert p.json()["status"] == "confirmed"
    assert p.json()["approval"]["status"] == "approved"
    assert p.json()["approval"]["comment"] == "Fine for Monday <b>ok</b>"


def make_headers(order: dict) -> dict:
    return _parties[order["order_id"]]


def test_api17_reuse_token_changes_nothing(client, catalog, clean_db):
    o = pending_order(client, make_party())
    token = token_of(o["order_id"])
    assert decide(client, token, "approve").status_code == 303
    before = state(o["order_id"])
    r = decide(client, token, "decline", "changed my mind")
    assert r.status_code == 303
    assert r.headers["location"] == f"/approve/{token}/done?outcome=already_decided"
    page = client.get(r.headers["location"])
    assert "This request was already decided." in page.text
    after = state(o["order_id"])
    assert after["order"] == "confirmed" and after["approval"] == "approved"
    assert after["comment"] is None and after["decided_at"] == before["decided_at"]
    assert len(after["events"]) == len(before["events"])
    # GET on a used token explains instead of showing the form
    g = client.get(f"/approve/{token}")
    assert g.status_code == 200 and "already decided" in g.text and "Approve order" not in g.text


def test_api18_decline_releases_holds(client, catalog, clean_db):
    o = pending_order(client, make_party())
    token = token_of(o["order_id"])
    r = decide(client, token, "decline")
    assert r.status_code == 303
    s = state(o["order_id"])
    assert s["order"] == "declined" and s["approval"] == "declined"
    assert s["holds"] == ["released"]
    assert [e["type"] for e in s["events"][-3:]] == ["approval.decided", "order.closed",
                                                     "holds.changed"]
    assert s["events"][-3]["text"] == "Maria Virtanen declined the order."
    page = client.get(r.headers["location"])
    assert "Declined. The agent has been told and the stock is released." in page.text


def test_api19_approve_after_expiry(client, catalog, clean_db):
    o = pending_order(client, make_party())
    token = token_of(o["order_id"])
    timeutil.shift_clock(timedelta(seconds=get_settings().approval_ttl_seconds + 60))
    g = client.get(f"/approve/{token}")
    assert "can no longer be approved" in g.text and "Approve order" not in g.text
    assert state(o["order_id"])["order"] == "pending_approval"  # GET changes nothing
    r = decide(client, token, "approve")
    assert r.headers["location"] == f"/approve/{token}/done?outcome=expired"
    s = state(o["order_id"])
    assert s["order"] == "expired" and s["approval"] == "expired" and s["holds"] == ["released"]
    types = [e["type"] for e in s["events"]]
    assert "approval.decided" not in types and "order.confirmed" not in types
    assert types[-3:] == ["approval.expired", "order.closed", "holds.changed"]
    page = client.get(r.headers["location"])
    assert re.search(r"This request expired at \d\d:\d\d and can no longer be approved\.",
                     page.text)
    # a second try still changes nothing
    n = len(s["events"])
    decide(client, token, "approve")
    assert len(state(o["order_id"])["events"]) == n


def test_decide_approval_function_results(client, catalog, clean_db):
    assert orders.decide_approval("nope", "approve", None)["result"] == "not_found"
    o = pending_order(client, make_party(), note=None)
    token = token_of(o["order_id"])
    res = orders.decide_approval(token, "approve", "x" * 600)
    assert res["result"] == "approved" and res["order_status"] == "confirmed"
    assert len(state(o["order_id"])["comment"]) == 500
    assert orders.decide_approval(token, "decline", None)["result"] == "already_decided"


def test_pending_approvals_lists_open_only(client, catalog, clean_db):
    o = pending_order(client, make_party())
    with db.connection() as conn:
        p = orders.pending_approvals(conn)
    assert [x["order_id"] for x in p] == [o["order_id"]]
    assert p[0]["customer_name"] == "Nordlys Hotels" and p[0]["totals"]["excl_tax"] == "1010.00"
    decide(client, token_of(o["order_id"]), "approve")
    with db.connection() as conn:
        assert orders.pending_approvals(conn) == []


# ---------------------------------------------------------------- admin


def test_admin_approval_link(client, catalog, clean_db, admin):
    o = pending_order(client, make_party())
    r = client.get(f"/v1/admin/orders/{o['order_id']}/approval-link", headers=admin)
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["order_id"] == o["order_id"] and body["status"] == "pending"
    assert body["approval_url"] == f"{get_settings().public_base_url}/approve/" + token_of(
        o["order_id"])
    assert body["expires_at"] == o["approval"]["expires_at"]
    assert client.get(f"/v1/admin/orders/{o['order_id']}/approval-link").status_code == 403
    r = client.get("/v1/admin/orders/ord_aaaaaaaaaaaaaaaaaaaa/approval-link", headers=admin)
    assert r.status_code == 404
    # a harness can approve with it
    path = body["approval_url"][len(get_settings().public_base_url):]
    assert client.post(path, data={"decision": "approve"}, follow_redirects=False
                       ).status_code == 303
    assert state(o["order_id"])["order"] == "confirmed"
    r = client.get(f"/v1/admin/orders/{o['order_id']}/approval-link", headers=admin)
    assert r.json()["status"] == "approved"


def test_admin_reset_forbidden_without_demo_mode(client, clean_db, admin, no_demo_mode):
    r = client.post("/v1/admin/reset", headers=admin)
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    assert client.post("/v1/admin/reset").status_code == 403


def test_admin_reset_in_demo_mode(client, catalog, clean_db, admin, demo_mode):
    if importlib.util.find_spec("handshake.demo") is None:
        pytest.skip("handshake/demo.py not written yet")
    p = make_party()
    pending_order(client, p)
    r = client.post("/v1/admin/reset", headers=admin)
    assert r.status_code == 200, r.text
    with db.connection() as conn:
        n = {t: db.q(conn, f"SELECT COUNT(*) FROM {t}").scalar_one()
             for t in ("orders", "offers", "events", "approvals", "stock_holds",
                       "products", "agents", "mandates")}
    assert n["orders"] == n["offers"] == n["events"] == n["approvals"] == n["stock_holds"] == 0
    assert n["products"] > 0 and n["agents"] >= 1 and n["mandates"] >= 1


# ---------------------------------------------------------------- console


def test_console_key_sets_cookie_and_redirects(client, catalog, clean_db, admin, demo_mode):
    o = pending_order(client, make_party())
    assert client.get("/console").status_code == 403
    r = client.get("/console?key=wrong", follow_redirects=False)
    assert r.status_code == 403 and "set-cookie" not in r.headers
    r = client.get("/console", params={"key": admin["X-Admin-Key"]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/console"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie.replace("Strict", "strict")
    assert "Path=/console" in cookie and admin["X-Admin-Key"] not in cookie
    page = client.get("/console")  # TestClient keeps the cookie
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    html = page.text
    assert "Nordlys Hotels" in html and "<svg" in html and 'class="qr"' in html
    assert "Open approval page" in html
    assert f"{get_settings().public_base_url}/approve/{token_of(o['order_id'])}" in html
    assert "approval.requested" in html and "Valio Aimo only" in html
    live = client.get("/console/live")
    assert live.status_code == 200 and "<svg" in live.text
    client.cookies.clear()
    assert client.get("/console/live").status_code == 403


def test_console_hides_qr_outside_demo_mode(client, catalog, clean_db, admin, no_demo_mode):
    pending_order(client, make_party())
    client.get("/console", params={"key": admin["X-Admin-Key"]})
    html = client.get("/console").text
    assert "Nordlys Hotels" in html and "<svg" not in html and "/approve/" not in html



def test_human_pages_load_no_external_resources(client, catalog, clean_db):
    """No web fonts or other third-party requests (decided 2026-11-14)."""
    o = pending_order(client, make_party())
    r = client.get(f"/approve/{token_of(o['order_id'])}")
    assert r.status_code == 200
    for marker in ("googleapis", "gstatic", "<link", "src=\"http"):
        assert marker not in r.text, marker
    csp = r.headers.get("content-security-policy", "")
    assert "googleapis" not in csp and "gstatic" not in csp
