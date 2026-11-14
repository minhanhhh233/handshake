"""Public landing page and guided demo (document 09, section 6a)."""

from __future__ import annotations

import re
from datetime import timedelta
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from handshake import db, public_demo, timeutil, upstream
from handshake.config import get_settings

from .fakes import FakeUpstream, make_facts

pytestmark = pytest.mark.db


def _seed():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import seed_demo

    with db.transaction() as conn:
        return seed_demo.seed(conn)


def _deliverable(days: int = 2):
    return timeutil.today_helsinki() + timedelta(days=days)


@pytest.fixture
def client(catalog, clean_db):
    from handshake.api import create_app

    _seed()
    d = _deliverable()
    closing = timeutil.now() + timedelta(days=1)
    facts = [make_facts(pid, price="1.01" if pid == "464306" else "1.60",
                        comparison="1.01" if pid == "464306" else "1.60", amount=None,
                        available_date=d, closing=closing)
             for pid in public_demo.SHORTLIST]
    upstream.set_adapter(FakeUpstream(facts))
    yield TestClient(create_app(), follow_redirects=False)
    upstream.set_adapter(None)


def run(client, scenario, ip="198.51.100.7"):
    r = client.post("/demo/run", data={"scenario": scenario}, headers={"X-Forwarded-For": ip})
    assert r.status_code == 303, r.text[:300]
    loc = r.headers["location"]
    assert re.match(r"^/demo/runs/trc_[a-z2-7]{20}\?k=[A-Za-z0-9_-]{32}$", loc)
    return loc


def test_landing_page(client):
    r = client.get("/classic")
    assert r.status_code == 200
    html = r.text
    for href in ('href="/docs"', 'href="/.well-known/handshake.json"',
                 'href="/.well-known/jwks.json"'):
        assert href in html
    assert 'id="lv-scripted"' in html and 'id="demo"' not in html  # stage 2 chips
    csp = r.headers["content-security-policy"]
    # One self-hosted script only (document 03, section 8): no inline script, no other origin.
    assert "script-src 'self'" in csp and "default-src 'none'" in csp
    assert "unsafe-inline" not in csp.split("script-src", 1)[1].split(";", 1)[0]
    assert "connect-src 'self'" in csp
    assert '<script src="/static/site.js" defer></script>' in html
    assert len(re.findall(r"<script", html)) == 1
    for section in ('id="playground"', 'id="discovery"', 'id="quick-start"', 'href="/llms.txt"'):
        assert section in html
    assert get_settings().admin_key not in html and "/console" not in html


def test_example_order_end_to_end(client):
    loc = run(client, "example_order")
    page = client.get(loc)
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    html = page.text
    titles = re.findall(r"<h2>(.*?)</h2>", html)
    assert titles[:6] == [
        "The agent reads its mandate", "The agent searches the catalogue",
        "Earliest delivery date from Valio Aimo", "Signed, fixed-price offer",
        "Dry run against the mandate: needs approval", "Order placed: pending approval"]
    assert _deliverable().isoformat() in html  # earliest date from upstream, not a fixed Monday
    assert "Signature verified with the published key" in html
    assert "spend_cap_exceeded" in html or "exceeds the mandate cap" in html
    approve = re.search(r'href="([^"]+/approve/[A-Za-z0-9_-]{43})"', html).group(1)
    assert approve.startswith(get_settings().public_base_url + "/approve/")
    assert "<svg" in html and 'http-equiv="refresh"' in html
    assert get_settings().admin_key not in html
    # The visitor approves; the run page then shows the confirmed order and the receipt link.
    r = client.post(urlparse(approve).path, data={"decision": "approve"})
    assert r.status_code == 303
    html = client.get(loc).text
    assert "Approved. The order is confirmed." in html and "/approve/" not in html
    assert 'http-equiv="refresh"' not in html
    receipt = re.search(r'href="[^"]*(/receipts/ord_[a-z2-7]{20}\?t=[^"]+)"', html).group(1)
    rec = client.get(receipt.replace("&amp;", "&"))
    assert rec.status_code == 200 and "Record intact" in rec.text


def test_public_demo_orders_expire_after_30_minutes(client):
    run(client, "example_order")
    with db.connection() as conn:
        req, exp = db.q(conn, "SELECT requested_at, expires_at FROM approvals").one()
    assert abs((exp - req).total_seconds() - 1800) < 2


def test_allergy_guard_refused(client):
    html = client.get(run(client, "allergy_guard")).text
    assert "Refused by the gateway" in html and "Lactose-free is not milk-free." in html
    assert "/approve/" not in html
    with db.connection() as conn:
        assert db.q(conn, "SELECT COUNT(*) FROM orders").scalar_one() == 0


def test_anonymous_search_has_no_prices(client):
    html = client.get(run(client, "anonymous_search")).text
    assert "No prices, no stock for anonymous callers" in html
    assert "Unexpected" not in html


def test_rate_limit_per_visitor(client):
    for _ in range(public_demo.RUNS_PER_WINDOW):
        run(client, "anonymous_search", ip="203.0.113.9")
    r = client.post("/demo/run", data={"scenario": "anonymous_search"},
                    headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    run(client, "anonymous_search", ip="203.0.113.10")  # another visitor is unaffected
    timeutil.shift_clock(timedelta(seconds=public_demo.WINDOW_SECONDS + 5))
    run(client, "anonymous_search", ip="203.0.113.9")  # window passed


def test_run_page_needs_its_token_and_shows_only_its_order(client, catalog):
    from .factories import make_party

    loc = run(client, "anonymous_search")
    trace = loc.split("/")[3].split("?")[0]
    assert client.get(f"/demo/runs/{trace}?k=wrong").status_code == 404
    assert client.get(f"/demo/runs/{trace}").status_code == 404
    # A rehearsal (non-demo) trace is not a demo run even with a valid-looking token.
    p = make_party()
    offer = client.post("/v1/offers", headers=p.headers, json={
        "delivery_date": _deliverable().isoformat(),
        "lines": [{"product_ids": ["464306"], "quantity": {"value": 1000, "unit": "L"}}]}).json()
    other_trace = offer["trace_id"]
    tok = public_demo.run_token(other_trace)
    assert client.get(f"/demo/runs/{other_trace}?k={tok}").status_code == 404


def test_unknown_scenario_and_ip_parsing(client):
    r = client.post("/demo/run", data={"scenario": "drop_tables"})
    assert r.status_code == 400

    class Req:
        def __init__(self, xff):
            self.headers = {"x-forwarded-for": xff} if xff else {}
            self.client = type("C", (), {"host": "10.0.0.1"})()

    assert public_demo.client_ip(Req("1.2.3.4, 5.6.7.8")) == "5.6.7.8"
    assert public_demo.client_ip(Req(None)) == "10.0.0.1"


def test_earliest_delivery_date_skips_passed_cutoffs(catalog, clean_db):
    now = timeutil.now()
    early, late = _deliverable(1), _deliverable(3)
    upstream.set_adapter(FakeUpstream([
        make_facts("464306", available_date=early, closing=now - timedelta(hours=1)),
        make_facts("464398", available_date=late, closing=now + timedelta(days=2)),
    ]))
    try:
        assert public_demo.earliest_delivery_date(["464306", "464398"], "trc_" + "a" * 20,
                                                  None) == late
        upstream.set_adapter(FakeUpstream([
            make_facts("464306", available_date=early, closing=now - timedelta(hours=1))]))
        assert public_demo.earliest_delivery_date(["464306"], "trc_" + "b" * 20, None) is None
    finally:
        upstream.set_adapter(None)


def test_home_page(client):
    """The redesigned landing page (document 09, section 6j)."""
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "The wholesale interface built <span class=\"grad\">for AI purchasing agents</span>" in html
    assert "Start here" not in html
    assert "marketplace" not in html.lower()
    for point in ("Signed fixed-price offers", "Limits set by people", "Human approval",
                  "Open standards"):
        assert point in html
    assert html.count('href="/live"') == 2 and "See how it works in live action" in html
    for section in ('id="discovery"', 'id="quick-start"', 'href="/.well-known/jwks.json"'):
        assert section in html
    assert "All customers and people shown are fictional." in html
    assert len(re.findall(r"<script", html)) == 1
    assert get_settings().admin_key not in html and "/console" not in html


def test_live_page(client):
    r = client.get("/live")
    assert r.status_code == 200
    html = r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp.split("script-src", 1)[1].split(";", 1)[0]
    assert '<script src="/static/live.js" defer></script>' in html
    assert len(re.findall(r"<script", html)) == 1
    for ident in ("lv-form", "lv-prompt", "frame-buyer", "frame-gw", "lv-human", "pb-count",
                  "pb-prev", "pb-play", "pb-next", "pb-speed", "pb-jump", "pb-all", "log",
                  "scen-groups", "rules-edit", "edit-box", "role-panel", "lv-timer"):
        assert f'id="{ident}"' in html, ident
    for p in ("visitor", "hotel", "restaurant", "school", "hospital", "probation", "approver",
              "operations"):
        assert f'data-persona="{p}"' in html, p
    assert "External buyer agent <span class=\"sub-h\">(not part of Handshake)</span>" in html
    assert "Handshake: the Valio Aimo interface" in html
    js = client.get("/static/live.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert "innerHTML" not in js.text and "eval(" not in js.text
    assert client.get("/classic").status_code == 200
