"""Personas and per-visitor sandboxes (stage 1; document 09, section 6c)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from handshake import db, live_demo, sandbox, timeutil, upstream
from handshake.models import ProductFacts
from handshake.policy import mandate_product_reasons

from .factories import make_party
from .fakes import FakeUpstream, make_facts
from .test_claude_agent import FakeAnthropic, resp, text, tool
from .test_public_demo import _deliverable, client  # noqa: F401  (fixture re-export)

pytestmark = pytest.mark.db


def card(c, persona="hotel"):  # noqa: F811
    r = c.get(f"/demo/sandbox?persona={persona}", headers={"X-Forwarded-For": "198.51.100.5"})
    assert r.status_code == 200, r.text
    return r


def test_sandbox_cookie_and_cards(client):  # noqa: F811
    r = card(client)
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{sandbox.COOKIE}=sbx_") and "HttpOnly" in cookie
    assert "Path=/demo" in cookie and "SameSite=lax" in cookie.replace("Lax", "lax")
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert [p["slug"] for p in body["personas"]] == list(sandbox.PERSONAS)
    assert body["card"]["cap"] == "1000.00" and body["card"]["customer"] == "Revontuli Hotels"
    again = card(client)
    assert "set-cookie" not in again.headers  # the same sandbox is reused
    rules = {x["name"]: x["value"] for x in card(client, "restaurant").json()["card"]["rules"]}
    assert "300.00" in rules["Per order"] and "800.00" in rules["Per week"]
    assert rules["Delivery"] == "Mon, Tue, Wed, Thu, Fri"
    school = {x["name"]: x["value"] for x in card(client, "school").json()["card"]["rules"]}
    assert "milk (AM)" in school["Allergens"] and "tree nuts (AN)" in school["Allergens"]
    assert "unknown data is refused" in school["Allergens"] and "22701" in school["Categories"]
    hosp = {x["name"]: x["value"] for x in card(client, "hospital").json()["card"]["rules"]}
    assert hosp["Substitutes"] == "every substitute needs approval"
    assert card(client, "probation").json()["card"]["cap"] == "50.00"
    v = card(client, "visitor").json()["card"]
    assert v["anonymous"] is True and "rules" not in v
    assert client.get("/demo/sandbox?persona=admin").status_code == 400


def test_cap_edit_and_checks(client):  # noqa: F811
    card(client, "probation")
    r = client.post("/demo/sandbox/cap", json={"persona": "probation", "cap": "75"})
    assert r.status_code == 200 and r.json()["card"]["cap"] == "75.00"
    assert r.json()["card"]["mandate_version"] == 2
    for bad in ("-1", "0", "abc", "1000001", None):
        assert client.post("/demo/sandbox/cap", json={"persona": "probation", "cap": bad}
                           ).status_code == 400
    assert client.post("/demo/sandbox/cap", json={"persona": "visitor", "cap": "5"}
                       ).status_code == 400
    assert client.post("/demo/sandbox/cap", content="cap=5",
                       headers={"Content-Type": "application/x-www-form-urlencoded"}
                       ).status_code == 415
    fresh = TestClient(client.app)
    assert fresh.post("/demo/sandbox/cap", json={"persona": "probation", "cap": "75"}
                      ).status_code == 409


def test_sandboxes_are_isolated_and_expire(client):  # noqa: F811
    a, b = TestClient(client.app), TestClient(client.app)
    card(a)
    card(b)
    sa = sandbox.parse_cookie(a.cookies.get(sandbox.COOKIE))
    sb = sandbox.parse_cookie(b.cookies.get(sandbox.COOKIE))
    assert sa and sb and sa != sb
    pa, pb = sandbox.party(sa, "hotel"), sandbox.party(sb, "hotel")
    assert pa["agent_id"] != pb["agent_id"] and pa["customer_id"] != pb["customer_id"]
    # Tampered cookie: a new sandbox.
    c = TestClient(client.app)
    c.cookies.set(sandbox.COOKIE, sa + ".forged", path="/demo")
    assert "set-cookie" in card(c).headers
    # Expiry: the agent key stops working after 2 hours and a new sandbox is made.
    key = sandbox.agent_key(pa["agent_id"])
    assert client.get("/v1/mandates/current",
                      headers={"Authorization": f"Bearer {key}"}).status_code == 200
    timeutil.shift_clock(sandbox.TTL + timedelta(minutes=1))
    assert client.get("/v1/mandates/current",
                      headers={"Authorization": f"Bearer {key}"}).status_code == 401
    assert "set-cookie" in card(a).headers


def test_holds_are_counted_per_sandbox(catalog, clean_db):
    from handshake.api import create_app

    d = _deliverable()
    upstream.set_adapter(FakeUpstream([make_facts(
        "469363", price="2.48", comparison="2.48", amount="600", available_date=d,
        closing=timeutil.now() + timedelta(days=1))]))
    try:
        app = create_app()
        s1, s2 = sandbox.create("10.0.0.1"), sandbox.create("10.0.0.2")
        keys = [sandbox.agent_key(sandbox.party(s["sandbox_id"], "hotel")["agent_id"])
                for s in (s1, s2)]
        real1, real2 = make_party(), make_party(name="Second real")
        c = TestClient(app)

        def take(key):
            r = c.post("/v1/offers", headers={"Authorization": f"Bearer {key}"}, json={
                "delivery_date": d.isoformat(),
                "lines": [{"product_ids": ["469363"], "quantity": {"value": 600, "unit": "L"}}]})
            if r.status_code != 201:
                return 0
            return r.json()["options"][0]["offer"]["lines"][0]["quantity"]["value"]

        assert take(keys[0]) == 600
        assert take(keys[1]) == 600   # another sandbox does not see sandbox 1's hold
        assert take(real1.key) == 600  # nor do real customers
        assert take(real2.key) == 0    # real customers still share real holds
    finally:
        upstream.set_adapter(None)


def test_live_run_uses_the_persona_and_stays_in_its_sandbox(client):  # noqa: F811
    s1, s2 = sandbox.create("10.1.0.1"), sandbox.create("10.1.0.2")
    fake = FakeAnthropic([resp(tool("get_mandate")), resp(text("ok"), stop="end_turn")])
    trace = live_demo.start("milk", "10.1.0.1", anthropic_client=fake, wait=True,
                            persona="restaurant", sandbox_id=s1["sandbox_id"])
    v = live_demo.view(trace, sandbox_id=s1["sandbox_id"])
    mandate = next(s for s in v["steps"] if s["kind"] == "result")["detail"]
    assert mandate["limits"]["max_weekly_total_excl_tax"] == "800.00"
    assert v["persona"] == "restaurant"
    assert "Bistro Kallio" in fake.requests[0]["system"][0]["text"]
    assert live_demo.view(trace, sandbox_id=s2["sandbox_id"]) is None
    assert live_demo.view(trace, sandbox_id=None) is None


def test_visitor_persona_is_refused_with_how_to_register(client):  # noqa: F811
    s = sandbox.create("10.2.0.1")
    fake = FakeAnthropic([
        resp(tool("search_products", q="milk", limit=3), tool("get_mandate"),
             tool("request_offer", delivery_date=_deliverable().isoformat(),
                  lines=[{"product_ids": ["464306"], "quantity": {"value": 10, "unit": "L"}}])),
        resp(text("Refused: needs a key."), stop="end_turn")])
    trace = live_demo.start("milk", "10.2.0.1", anthropic_client=fake, wait=True,
                            persona="visitor", sandbox_id=s["sandbox_id"])
    v = live_demo.view(trace, sandbox_id=s["sandbox_id"])
    res = {s_["title"]: s_ for s_ in v["steps"] if s_["kind"] == "result"}
    assert res["search_products"]["status"] == "ok"
    for name in ("get_mandate", "request_offer"):
        assert res[name]["status"] == "bad" and res[name]["detail"]["status"] == 401
        assert "Register the agent with Valio Aimo" in res[name]["detail"]["next"]


def test_live_route_needs_a_sandbox_and_a_known_persona(client, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    fresh = TestClient(client.app)
    assert fresh.post("/demo/live", json={"prompt": "milk"}).status_code == 409
    card(fresh)
    assert fresh.post("/demo/live", json={"prompt": "milk", "persona": "admin"}
                      ).status_code == 400


def test_forbidden_category_rule():
    m = sandbox._mandate_body("school")
    from handshake.models import Mandate

    mandate = Mandate(**m.dump(), mandate_id="mnd_" + "a" * 20, version=1,
                      agent_id="agt_" + "a" * 20, customer_id="cus_" + "a" * 20)
    facts = ProductFacts(product_id="460001", category_code=sandbox.SCHOOL_FORBIDDEN_CATEGORY,
                         allergens={"AM": "FREE_FROM", "AN": "FREE_FROM"},
                         has_allergen_data=True, claims=[])
    reasons = mandate_product_reasons(facts, mandate)
    assert [r.code for r in reasons] == ["category_not_allowed"] and reasons[0].rule == "R2"
    assert db is not None


def test_run_page_of_a_sandbox_run_needs_that_sandbox(client):  # noqa: F811
    card(client)
    sid = sandbox.parse_cookie(client.cookies.get(sandbox.COOKIE))
    fake = FakeAnthropic([resp(text("ok"), stop="end_turn")])
    trace = live_demo.start("milk", "10.3.0.1", anthropic_client=fake, wait=True,
                            persona="hotel", sandbox_id=sid)
    from handshake import public_demo

    url = f"/demo/runs/{trace}?k={public_demo.run_token(trace)}"
    assert client.get(url).status_code == 200
    other = TestClient(client.app)
    assert other.get(url).status_code == 404
    card(other)
    assert other.get(url).status_code == 404
