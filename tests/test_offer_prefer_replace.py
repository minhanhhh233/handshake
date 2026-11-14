"""`prefer: lowest_price` on offer lines and `replaces_offer_id` (stage 0, 2026-11-14;
document 03, section 5; document 05, section 4), plus the live agent's one retry."""

from __future__ import annotations

import json

import pytest

from handshake import db, live_demo, public_demo

from .factories import make_party
from .test_claude_agent import FakeAnthropic, resp, text, tool
from .test_public_demo import _deliverable, client  # noqa: F401  (fixture re-export)

pytestmark = pytest.mark.db


def offer(client, party, ids, prefer=None, **extra):  # noqa: F811
    line = {"product_ids": ids, "quantity": {"value": 1000, "unit": "L"}}
    if prefer:
        line["prefer"] = prefer
    return client.post("/v1/offers", headers=party.headers, json={
        "delivery_date": _deliverable().isoformat(), "lines": [line], **extra})


def test_lowest_price_fills_from_the_cheapest(client):  # noqa: F811
    p = make_party()
    ids = ["464398", "464201", "464306"]  # 464306 is the cheapest (1.01 vs 1.60 per L)
    listed = offer(client, p, ids).json()["options"][0]["offer"]
    assert listed["lines"][0]["product_id"] == "464398"
    cheap = offer(client, p, ids, prefer="lowest_price").json()["options"][0]["offer"]
    assert [ln["product_id"] for ln in cheap["lines"]] == ["464306"]
    assert cheap["totals"]["excl_tax"] == "1010.00"
    assert cheap["lines"][0]["role"] == "alternate"  # roles still follow the agent's list
    with db.connection() as conn:
        body = db.q(conn, "SELECT body_json FROM events WHERE type = 'offer.requested' "
                          "ORDER BY at DESC, seq DESC LIMIT 1").scalar_one()
    assert json.loads(body)["lines"][0]["prefer"] == "lowest_price"
    bad = offer(client, p, ids, prefer="cheapest_please")
    assert bad.status_code == 400


def test_replaces_offer_withdraws_and_releases(client):  # noqa: F811
    p = make_party()
    first = offer(client, p, ["464306"]).json()["options"][0]["offer"]["offer_id"]
    with db.connection() as conn:
        held = db.q(conn, "SELECT COUNT(*) FROM stock_holds WHERE offer_id = :o AND "
                          "status = 'held'", o=first).scalar_one()
    second = offer(client, p, ["464306"], replaces_offer_id=first)
    assert second.status_code == 201
    with db.connection() as conn:
        status = db.q(conn, "SELECT status FROM offers WHERE offer_id = :o", o=first).scalar_one()
        still = db.q(conn, "SELECT COUNT(*) FROM stock_holds WHERE offer_id = :o AND "
                           "status = 'held'", o=first).scalar_one()
        ev = db.q(conn, "SELECT body_json FROM events WHERE type = 'offer.expired'").all()
    assert status == "expired" and still == 0 and held >= 0
    assert any(json.loads(b[0]).get("reason") == "replaced" for b in ev)
    # The withdrawn offer cannot be ordered.
    r = client.post("/v1/orders", headers={**p.headers, "Idempotency-Key": "replaced-offer-1"},
                    json={"offer_id": first})
    assert r.status_code in (409, 410), r.text
    # A malformed ID is invalid.
    assert offer(client, p, ["464306"], replaces_offer_id="nope").status_code == 400


def test_replacing_someone_elses_offer_is_not_found(client):  # noqa: F811
    p, other = make_party(), make_party(name="Other")
    oid = offer(client, p, ["464306"]).json()["options"][0]["offer"]["offer_id"]
    r = offer(client, other, ["464306"], replaces_offer_id=oid)
    assert r.status_code == 404
    with db.connection() as conn:
        assert db.q(conn, "SELECT status FROM offers WHERE offer_id = :o",
                    o=oid).scalar_one() == "active"


def _last_offer_id(req):
    for b in req["messages"][-1]["content"]:
        body = json.loads(b["content"])
        for o in body.get("options") or []:
            return o["offer"]["offer_id"]
    return None


def test_live_agent_retries_once_and_replaces(client, monkeypatch):  # noqa: F811
    d = _deliverable().isoformat()
    line = {"product_ids": ["464306"], "quantity": {"value": 100, "unit": "L"}}
    seen: dict[str, str] = {}

    def second(req):
        seen["first"] = _last_offer_id(req)
        return resp(tool("request_offer", delivery_date=d, lines=[line]))

    fake = FakeAnthropic([
        resp(text("Plan:\n- milk: 100 L"), tool("request_offer", delivery_date=d, lines=[line])),
        second,
        resp(tool("request_offer", delivery_date=d, lines=[line])),  # a third: refused
        resp(text("done"), stop="end_turn"),
    ])
    trace = live_demo.start("100 litres of lactose-free milk at the best price",
                            "198.51.100.90", anthropic_client=fake, wait=True)
    v = live_demo.view(trace)
    calls = [s for s in v["steps"] if s["kind"] == "call" and s["title"] == "request_offer"]
    results = [s for s in v["steps"] if s["kind"] == "result" and s["title"] == "request_offer"]
    assert "replaces_offer_id" not in calls[0]["request"]["input"]
    assert calls[1]["request"]["input"]["replaces_offer_id"] == seen["first"]
    assert results[2]["status"] == "bad" and results[2]["detail"]["error"] == "retry_limit"
    with db.connection() as conn:
        assert db.q(conn, "SELECT status FROM offers WHERE offer_id = :o",
                    o=seen["first"]).scalar_one() == "expired"
    sys_prompt = fake.requests[0]["system"][0]["text"]
    assert "lowest_price" in sys_prompt and "retry" in sys_prompt
    assert public_demo is not None
