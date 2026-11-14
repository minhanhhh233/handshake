"""Receipt page, buyer and Valio Aimo views (task T-403; document 03, section 8; A-23)."""

from __future__ import annotations

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


def delivery():
    d = timeutil.today_helsinki() + timedelta(days=2)
    while d.isoweekday() == 7:
        d += timedelta(days=1)
    return d


class RecordingUpstream(FakeUpstream):
    """FakeUpstream that also writes the `upstream.queried` event the real adapter writes
    (aimo_only, own transaction, snapshot ids in the body; handshake/upstream.py)."""

    next_id = 9000

    def get_facts(self, product_ids, delivery_date, *, trace_id, actor_id=None):  # noqa: ANN001
        out = super().get_facts(product_ids, delivery_date, trace_id=trace_id,
                                actor_id=actor_id)
        ids = list(product_ids)
        RecordingUpstream.next_id += 1
        n = RecordingUpstream.next_id
        with db.transaction() as conn:
            events.append(
                conn, trace_id=trace_id, type="upstream.queried", actor_type="gateway",
                audience="aimo_only",
                text=f"Fetched price and availability for {len(ids)} product(s); 0 served "
                     "from cache.",
                body={"delivery_date": delivery_date.isoformat(), "product_ids": ids,
                      "fetched": ids, "cache_hit": [], "missing": [], "agent_id": actor_id,
                      "price_quote_ids": {p: n for p in ids},
                      "availability_snapshot_ids": {p: n + 500 for p in ids}})
        return out


def fake_upstream() -> RecordingUpstream:
    return RecordingUpstream([make_facts(
        PID, price="1.01", comparison="1.01", amount=None, available_date=delivery(),
        closing=timeutil.now() + timedelta(days=1))])


@pytest.fixture
def admin_key():
    key = get_settings().admin_key
    if not key:
        pytest.skip("ADMIN_KEY not set")
    return key


@pytest.fixture
def client(admin_key):
    from handshake.api import create_app

    upstream.set_adapter(fake_upstream())
    yield TestClient(create_app())
    upstream.set_adapter(None)


def approved_order(client) -> dict:
    p = make_party()
    r = client.post("/v1/offers", headers=p.headers, json={
        "delivery_date": delivery().isoformat(),
        "lines": [{"product_ids": [PID], "quantity": {"value": 1000, "unit": "L"}}]})
    assert r.status_code == 201, r.json()
    offer_id = r.json()["options"][0]["offer"]["offer_id"]
    r = client.post("/v1/orders", headers=p.headers | {"Idempotency-Key": "key-rpage-1"},
                    json={"offer_id": offer_id, "reference": "PO-<i>7</i>", "note": NOTE})
    assert r.status_code == 202, r.json()
    o = r.json()
    with db.connection() as conn:
        a = orders.load_approval(conn, o["order_id"])
    orders.decide_approval(orders.approval_token(a["approval_id"]), "approve",
                           "Fine <b>this once</b>")
    with db.connection() as conn:
        row = orders.load_order(conn, o["order_id"])
        evs = events.list_events(conn, o["trace_id"])
    return {**o, "token": row["receipt_token"], "events": evs, "headers": p.headers}


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).replace("&#39;", "'")


def check_headers(r) -> None:
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-frame-options"] == "DENY"
    csp = r.headers["content-security-policy"]
    assert "script-src" not in csp and "default-src 'none'" in csp


def test_buyer_view(client, catalog, clean_db):
    o = approved_order(client)
    r = client.get(f"/receipts/{o['order_id']}", params={"t": o["token"]})
    assert r.status_code == 200, r.text
    check_headers(r)
    html, text = r.text, text_of(r.text)
    n = len(o["events"])
    hidden = sum(e["audience"] == "aimo_only" for e in o["events"])
    assert hidden >= 2
    assert "Ordered 1,000 L of" in text and "Approved by Maria Virtanen." in text
    kid = get_settings().signing_kid
    assert f"Signed by the gateway (key {kid}). Record intact: {n} events." in text
    assert (f"This view leaves out {hidden} internal Valio Aimo events. The chain covers "
            "them, so the record above is still complete.") in text
    # chain head as a fragment, full value available
    head = o["events"][-1]["hash"]
    assert f"{head[:8]}…{head[-4:]}" in html and head in html
    # buyer sees no internal events
    assert "Valio Aimo only" not in html and "upstream" not in html.lower()
    for e in o["events"]:
        if e["audience"] == "both":
            assert e["text"].split(" (")[0][:40] in text_of(html), e["type"]
    assert "Your agent" in text and "Handshake gateway" in text
    # the order of events follows seq
    seqs = [int(s) for s in re.findall(r'class="seq" aria-label="Event (\d+)"', html)]
    assert seqs == sorted(seqs) and len(seqs) == n - hidden
    # untrusted text: escaped and labelled; tokens never echoed
    assert "<script>alert(1)</script>" not in html and "<b>this once</b>" not in html
    assert "&lt;script&gt;" in html and "&lt;b&gt;this once&lt;/b&gt;" in html
    assert "Note from the agent (not verified)" in text
    assert "Comment from Maria Virtanen (not verified)" in text
    assert "Order reference from the agent (not verified)" in text
    assert o["token"] not in html and get_settings().admin_key not in html
    # footer
    assert o["offer_id"] in html and "Show the signed offer" in html
    assert "/.well-known/jwks.json" in html
    with db.connection() as conn:
        jws = db.q(conn, "SELECT jws FROM offers WHERE offer_id = :o",
                   o=o["offer_id"]).scalar_one()
    assert jws in html


def test_wrong_or_missing_token_is_404(client, catalog, clean_db):
    o = approved_order(client)
    for params in ({"t": "x" * 43}, {}, {"t": ""}, {"t": o["token"][:-1]}):
        r = client.get(f"/receipts/{o['order_id']}", params=params)
        assert r.status_code == 404, params
        check_headers(r)
        assert "not valid" in r.text and o["order_id"] not in r.text
    # unknown order: same page
    unknown = client.get("/receipts/ord_aaaaaaaaaaaaaaaaaaaa", params={"t": o["token"]})
    assert unknown.status_code == 404 and unknown.text == r.text
    assert client.get("/receipts/not-an-id").status_code == 404


def test_admin_key_cookie_and_aimo_view(client, catalog, clean_db, admin_key):
    o = approved_order(client)
    r = client.get(f"/receipts/{o['order_id']}", params={"key": "wrong"},
                   follow_redirects=False)
    assert r.status_code == 404 and "set-cookie" not in r.headers
    r = client.get(f"/receipts/{o['order_id']}", params={"key": admin_key},
                   follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/receipts/{o['order_id']}"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "samesite=strict" in cookie.lower()
    assert "Path=/receipts" in cookie and admin_key not in cookie
    check_headers(r)

    page = client.get(f"/receipts/{o['order_id']}")  # TestClient keeps the cookie
    assert page.status_code == 200
    check_headers(page)
    html, text = page.text, text_of(page.text)
    internal = [e for e in o["events"] if e["audience"] == "aimo_only"]
    assert text.count("Valio Aimo only") == len(internal)
    assert "This view leaves out" not in text
    seqs = [int(s) for s in re.findall(r'class="seq" aria-label="Event (\d+)"', html)]
    assert seqs == list(range(1, len(o["events"]) + 1))
    # upstream snapshot ids from the upstream.queried body
    q = next(e for e in o["events"] if e["type"] == "upstream.queried")
    pq = q["body"]["price_quote_ids"][PID]
    av = q["body"]["availability_snapshot_ids"][PID]
    assert pq and av
    assert f"Product {PID}: price quote {pq}, availability snapshot {av}" in text
    assert "Nordlys Hotels purchasing agent" in text
    assert admin_key not in html and o["token"] not in html

    # the cookie works for /receipts only, not for the console
    assert client.get("/console").status_code == 403


def test_tampered_event_shows_altered_line(client, catalog, clean_db):
    o = approved_order(client)
    target = next(e for e in o["events"] if e["type"] == "policy.evaluated")
    with db.transaction() as conn:
        db.q(conn, "UPDATE events SET body_json = :b WHERE event_id = :e",
             b=db.jdump({**target["body"], "outcome": "accepted"}), e=target["event_id"])
    r = client.get(f"/receipts/{o['order_id']}", params={"t": o["token"]})
    assert r.status_code == 200
    text = text_of(r.text)
    assert (f"Record altered at event {target['seq']}: the receipt does not match the log."
            in text)
    assert "Record intact" not in text
    assert "This event no longer matches its recorded hash." in text


def test_tampered_internal_event_is_caught_in_buyer_view(client, catalog, clean_db):
    o = approved_order(client)
    target = next(e for e in o["events"] if e["type"] == "upstream.queried")
    with db.transaction() as conn:
        db.q(conn, "UPDATE events SET body_json = :b WHERE event_id = :e",
             b=db.jdump({**target["body"], "missing": ["x"]}), e=target["event_id"])
    text = text_of(client.get(f"/receipts/{o['order_id']}", params={"t": o["token"]}).text)
    assert f"Record altered at event {target['seq']}" in text


def test_receipt_page_loads_no_external_resources(client, catalog, clean_db):
    o = approved_order(client)
    html = client.get(f"/receipts/{o['order_id']}", params={"t": o["token"]}).text
    for marker in ("googleapis", "gstatic", "<link", "src=\"http", "<script", "<img",
                   "@import", "url("):
        assert marker not in html, marker
