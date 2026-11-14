"""Landing-page playground, discovery, llms.txt and the script policy (document 09, section 6a)."""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import pytest

from handshake import db, public_demo
from handshake.config import get_settings

from .test_public_demo import client, run  # noqa: F401  (fixture re-export)

pytestmark = pytest.mark.db
IP = "198.51.100.40"


def pg(client, step, session=None, ip=IP):  # noqa: F811
    return client.post(f"/demo/playground/{step}", json=session or {},
                       headers={"X-Forwarded-For": ip})


def _no_secrets(text: str) -> None:
    s = get_settings()
    assert s.admin_key not in text
    assert "hsk_" not in text  # no agent key of any kind
    assert "/console" not in text


def test_playground_runs_the_example_order(client):  # noqa: F811
    r = pg(client, "search")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    sess = body["session"]
    assert body["tool"] == "search_products" and body["request"]["http"] == "GET /v1/products"
    assert body["response"]["items"] and len(body["response"]["items"]) <= 5
    _no_secrets(r.text)

    offer = pg(client, "request_offer", sess).json()
    assert offer["session"] == sess and offer["tool"] == "request_offer"
    jws = offer["response"]["options"][0]["jws"]
    again = pg(client, "request_offer", sess).json()  # one offer per session
    assert again["response"]["options"][0]["jws"] == jws and "note" in again

    v = pg(client, "verify_signature", sess).json()
    assert v["response"]["verified"] is True and v["response"]["header"]["alg"] == "EdDSA"
    assert v["request"]["jws"] == jws

    dry = pg(client, "dry_run", sess).json()
    assert dry["response"]["outcome"] == "needs_approval"

    placed = pg(client, "place_order", sess)
    po = placed.json()
    assert po["response"]["status"] == "pending_approval"
    # The agent's view never carries the approval link; only the visitor's own block does.
    assert "/approve/" not in json.dumps(po["response"])
    url = po["you"]["approval_url"]
    assert url.startswith(get_settings().public_base_url + "/approve/")
    _no_secrets(placed.text)
    with db.connection() as conn:
        n = db.q(conn, "SELECT COUNT(*) FROM orders WHERE trace_id = :t",
                 t=sess["trace_id"]).scalar_one()
    assert n == 1
    assert pg(client, "place_order", sess).json()["response"]["order_id"] == \
        po["response"]["order_id"]  # same idempotency key: no second order

    assert client.post(urlparse(url).path, data={"decision": "approve"}).status_code == 303
    rec = pg(client, "get_receipt", sess)
    assert rec.status_code == 200
    rb = rec.json()["response"]
    assert rb["jws"].count(".") == 2 and "confirmed" in json.dumps(rb["receipt"])
    _no_secrets(rec.text)

    # The session's run page shows each call with its request.
    page = client.get(f"/demo/runs/{sess['trace_id']}?k={sess['k']}")
    assert page.status_code == 200 and "Playground session" in page.text
    assert "Request / response" in page.text


def test_steps_need_their_prerequisites(client):  # noqa: F811
    for step in ("verify_signature", "dry_run", "place_order", "get_receipt"):
        r = pg(client, step)
        assert r.status_code == 409 and r.json()["code"] == "needs_request_offer"
    with db.connection() as conn:  # no session (no demo run) was started for those
        assert db.q(conn, "SELECT COUNT(*) FROM events WHERE type = 'demo.run'").scalar_one() == 0
    sess = pg(client, "search").json()["session"]
    assert pg(client, "dry_run", sess).status_code == 409
    pg(client, "request_offer", sess)
    r = pg(client, "get_receipt", sess)
    assert r.status_code == 409 and r.json()["code"] == "needs_place_order"


def test_session_token_and_scope(client):  # noqa: F811
    sess = pg(client, "search").json()["session"]
    assert pg(client, "search", {"trace_id": sess["trace_id"], "k": "x" * 32}).status_code == 404
    assert pg(client, "search", {"trace_id": "not-a-trace", "k": sess["k"]}).status_code == 404
    # A guided-demo run with a valid token is not a playground session.
    loc = run(client, "example_order", ip="198.51.100.41")
    trace, k = loc.split("/")[3].split("?k=")
    r = pg(client, "place_order", {"trace_id": trace, "k": k})
    assert r.status_code == 404 and r.json()["code"] == "unknown_session"


def test_new_sessions_share_the_visitor_limit(client):  # noqa: F811
    ip = "203.0.113.50"
    sess = pg(client, "search", ip=ip).json()["session"]
    for _ in range(public_demo.RUNS_PER_WINDOW - 1):
        run(client, "anonymous_search", ip=ip)
    r = pg(client, "search", ip=ip)  # a sixth run
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    # Steps in an existing session are not new runs.
    assert pg(client, "request_offer", sess, ip=ip).status_code == 200
    # Searches inside one session are capped.
    for _ in range(public_demo.MAX_SEARCHES_PER_SESSION - 1):
        assert pg(client, "search", sess, ip=ip).status_code == 200
    r = pg(client, "search", sess, ip=ip)
    assert r.status_code == 429 and r.json()["code"] == "too_many_searches"


def test_playground_input_checks(client):  # noqa: F811
    r = client.post("/demo/playground/search", content="trace_id=x",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415
    assert pg(client, "drop_tables").status_code == 400
    assert client.get("/demo/playground/search").status_code == 405


def test_llms_txt_and_site_js(client):  # noqa: F811
    r = client.get("/llms.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    base = get_settings().public_base_url
    assert r.text.startswith("# Handshake") and f"{base}/.well-known/handshake.json" in r.text
    assert "<agent key>" in r.text and "&lt;" not in r.text  # plain text, not HTML-escaped
    _no_secrets(r.text)
    js = client.get("/static/site.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith(
        "application/javascript")
    assert "innerHTML" not in js.text and "eval(" not in js.text


def test_script_policy_only_on_landing_and_run_pages(client):  # noqa: F811
    loc = run(client, "example_order", ip="198.51.100.42")
    page = client.get(loc)
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert '<script src="/static/site.js" defer></script>' in page.text
    assert "Request / response" in page.text and 'data-copy="rq-1"' in page.text
    approve = re.search(r'href="([^"]+/approve/[A-Za-z0-9_-]{43})"', page.text).group(1)
    a = client.get(urlparse(approve).path)
    assert a.status_code == 200
    assert "script-src" not in a.headers["content-security-policy"] and "<script" not in a.text
    client.post(urlparse(approve).path, data={"decision": "approve"})
    html = client.get(loc).text
    receipt = re.search(r'href="[^"]*(/receipts/ord_[a-z2-7]{20}\?t=[^"]+)"', html).group(1)
    rec = client.get(receipt.replace("&amp;", "&"))
    assert "script-src" not in rec.headers["content-security-policy"] and "<script" not in rec.text
