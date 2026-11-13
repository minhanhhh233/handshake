"""Scripted agent end to end through the real app (task T-304; document 07, section 5;
document 09, sections 4 and 6).

The scripted agent drives a real HandshakeClient whose transport is a FastAPI TestClient, so
every call goes through the gateway: search on the copied catalog, offers from FakeUpstream,
policy, orders and approvals in the test database. The test plays the human approver.
"""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent import scripted
from agent.handshake_client import HandshakeClient, SignatureVerificationError
from handshake import db, orders, timeutil, upstream

from .factories import make_party
from .fakes import FakeUpstream, make_facts

pytestmark = [pytest.mark.db]


def delivery():
    d = timeutil.today_helsinki() + timedelta(days=2)
    while d.isoweekday() == 7:  # mandate weekdays 1 to 6
        d += timedelta(days=1)
    return d


@pytest.fixture
def app():
    from handshake.api import create_app

    d = delivery()
    closing = timeutil.now() + timedelta(days=1)

    def f(pid, price, amount):
        return make_facts(pid, price=price, comparison=price, amount=amount,
                          available_date=d, closing=closing)

    upstream.set_adapter(FakeUpstream([
        f("464306", "1.01", None),
        f("469363", "2.48", "384"),
        f("464398", "1.65", None),
        f("464201", "1.56", None),
        f("462775", "2.57", "51"),
    ]))
    yield create_app()
    upstream.set_adapter(None)


def client_for(app, party) -> HandshakeClient:
    return HandshakeClient("http://testserver", party.key, http=TestClient(app))


def run(app, party, scenario="milk_order", hook=None, **kw):
    c = client_for(app, party)
    if hook is not None:
        hook(c)
    lines: list[str] = []
    summary = scripted.run(scenario, c, delivery(), kw.pop("poll_timeout", 5),
                           poll_interval=0.05, out=lambda *a, **k: lines.append(" ".join(map(str, a))),
                           **kw)
    return summary, "\n".join(lines)


def approve(app, order_id: str) -> None:
    """Act as the human: approve out of band, through the approval page when it exists."""
    with db.connection() as conn:
        a = orders.load_approval(conn, order_id)
    assert a is not None and a["status"] == "pending"
    token = orders.approval_token(a["approval_id"])
    paths = {getattr(r, "path", None) for r in app.routes}
    if "/approve/{token}" in paths:
        r = TestClient(app).post(f"/approve/{token}", data={"decision": "approve"},
                                 follow_redirects=False)
        assert r.status_code < 400, r.text
    elif hasattr(orders, "decide_approval"):
        orders.decide_approval(token, "approve", None)
    else:  # no approval flow yet: same effect directly
        now = timeutil.to_db(timeutil.now())
        with db.transaction() as conn:
            db.q(conn, "UPDATE approvals SET status='approved', decided_at=:d WHERE order_id=:o",
                 d=now, o=order_id)
            db.q(conn, "UPDATE orders SET status='confirmed', updated_at=:d WHERE order_id=:o",
                 d=now, o=order_id)
            row = orders.load_order(conn, order_id)
            orders.commit_holds(conn, row["offer_id"], row["trace_id"])


def approve_on_first_poll(app):
    def hook(c: HandshakeClient) -> None:
        original = c.get_order
        state = {"done": False}

        def get_order(order_id):
            if not state["done"]:
                state["done"] = True
                approve(app, order_id)
            return original(order_id)

        c.get_order = get_order  # type: ignore[method-assign]
    return hook


# ---------------------------------------------------------------- scenarios


def test_milk_order_under_cap_confirmed(app, catalog, clean_db):
    p = make_party(limits={"max_order_total_excl_tax": "5000.00"})
    s, text = run(app, p)
    assert s["error"] is None, text
    assert s["final_status"] == "confirmed" and s["outcome"] == "accepted"
    assert s["dry_run_outcome"] == "accepted"
    assert s["lines"] == [{"product_id": "464306", "role": "requested", "quantity": "1000",
                           "sales_unit": "ST", "assurance": "confirmed"}]
    assert s["trace_id"] and s["offer_id"] and s["order_id"]
    assert "signature verified (kid" in text
    assert "[1] get_mandate()" in text and "get_receipt(" in text
    with db.connection() as conn:
        statuses = [r[0] for r in db.q(conn, "SELECT status FROM stock_holds WHERE offer_id=:o",
                                       o=s["offer_id"])]
        trace = db.q(conn, "SELECT trace_id FROM orders WHERE order_id=:o",
                     o=s["order_id"]).scalar_one()
    assert statuses == ["committed"]
    assert trace == s["trace_id"]  # one trace per task


def test_milk_order_needs_approval_then_confirmed(app, catalog, clean_db):
    p = make_party()  # cap 1000.00 < 1010.00
    s, text = run(app, p, hook=approve_on_first_poll(app))
    assert s["error"] is None, text
    assert s["dry_run_outcome"] == "needs_approval"
    assert "spend_cap_exceeded" in text
    assert "Needs approval from Maria Virtanen" in text
    assert s["outcome"] == "needs_approval" and s["final_status"] == "confirmed"
    assert "/approve/" not in text  # the agent never sees an approval link (FR-35)


def test_milk_order_poll_timeout_leaves_pending(app, catalog, clean_db):
    p = make_party()
    s, text = run(app, p, poll_timeout=0.2)
    assert s["error"] is None
    assert s["final_status"] == "pending_approval"
    assert "Still pending_approval" in text


def test_milk_split_384_plus_616(app, catalog, clean_db):
    p = make_party(limits={"max_order_total_excl_tax": "5000.00"})
    s, text = run(app, p, scenario="milk_split")
    assert s["error"] is None, text
    got = [(ln["product_id"], ln["role"], ln["quantity"]) for ln in s["lines"]]
    assert got == [("469363", "requested", "384"), ("464306", "alternate", "616")]
    assert s["final_status"] == "confirmed"


def test_tampered_offer_stops_before_dry_run(app, catalog, clean_db, monkeypatch):
    p = make_party()
    calls: list[str] = []

    def hook(c: HandshakeClient) -> None:
        def bad(option):
            raise SignatureVerificationError("signature invalid: test tamper")

        c._verify_option = bad  # type: ignore[method-assign]
        orig = c.dry_run_order
        c.dry_run_order = lambda oid: calls.append(oid) or orig(oid)  # type: ignore[method-assign]

    s, text = run(app, p, hook=hook)
    assert s["error"]["type"] == "signature"
    assert "signature verification FAILED" in text and "stopping before dry-run" in text
    assert calls == [] and s["offer_id"] is None and s["order_id"] is None
    assert "Traceback" not in text


def test_api_error_is_readable(app, catalog, clean_db):
    p = make_party(forbidden_allergens={"codes": ["AM"], "states": ["CONTAINS", "MAY_CONTAIN"],
                                        "unknown": "reject"})
    s, text = run(app, p)
    assert s["error"]["type"] == "api" and s["error"]["code"] == "no_candidates"
    assert "ERROR: gateway answered 422 no_candidates" in text


# ---------------------------------------------------------------- unit


def test_next_monday():
    from datetime import date

    assert scripted.next_monday(date(2026, 11, 14)) == date(2026, 11, 16)  # Saturday
    assert scripted.next_monday(date(2026, 11, 16)) == date(2026, 11, 23)  # Monday -> next
    assert scripted.next_monday(date(2026, 11, 15)) == date(2026, 11, 16)


def test_key_resolution(monkeypatch):
    monkeypatch.setenv("HANDSHAKE_AGENT_KEY", "hsk_env")
    assert scripted.resolve_key("hsk_arg", None) == "hsk_arg"
    assert scripted.resolve_key(None, None) == "hsk_env"


def test_scripted_does_not_import_gateway_package():
    tree = ast.parse((Path(scripted.__file__)).read_text("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            mods = [node.module or ""]
        else:
            continue
        assert not any(m == "handshake" or m.startswith("handshake.") for m in mods), mods
