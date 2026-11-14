"""MCP server (task T-502; document 07, section 6).

The server runs in-process behind the MCP SDK's in-memory transport (`mcp.Client(server)`); its
HandshakeClient talks to the real app through a FastAPI TestClient, with FakeUpstream for prices
and stock. The test plays the human approver server-side.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from agent.claude_agent import tool_definitions
from agent.handshake_client import HandshakeClient
from mcp_server import server as mcp_server
from mcp_server.server import TOOL_NAMES, build_server

from .factories import make_party
from .fakes import FakeUpstream, make_facts


def delivery():
    from handshake import timeutil

    d = timeutil.today_helsinki() + timedelta(days=2)
    while d.isoweekday() == 7:  # mandate weekdays 1 to 6
        d += timedelta(days=1)
    return d


def _payload(result) -> Any:
    assert len(result.content) == 1 and result.content[0].type == "text"
    return json.loads(result.content[0].text)


async def _call(client: Client, name: str, args: dict[str, Any] | None = None):
    result = await client.call_tool(name, args or {})
    return _payload(result), result.is_error


# ---------------------------------------------------------------- no database


def test_list_tools_exactly_eight_with_schemas():
    hs = HandshakeClient("http://unused.invalid", "hsk_x")
    server = build_server(hs)
    assert hs.trace_id and hs.trace_id.startswith("trc_")  # one trace for the session
    expected = {t["name"]: t["input_schema"] for t in tool_definitions()}

    async def go():
        async with Client(server) as client:
            return (await client.list_tools()).tools

    tools = anyio.run(go)
    assert [t.name for t in tools] == list(TOOL_NAMES)
    assert set(TOOL_NAMES) == set(expected)
    for t in tools:
        assert t.input_schema == expected[t.name]
        assert t.description
    get_order = next(t for t in tools if t.name == "get_order")
    assert "every 5 seconds" in get_order.description and "never waits" in get_order.description


def test_signature_failure_and_scrub(monkeypatch):
    from agent.handshake_client import SignatureVerificationError

    hs = HandshakeClient("http://unused.invalid", "hsk_x")

    def bad(*a, **k):
        raise SignatureVerificationError("signature invalid")

    monkeypatch.setattr(hs, "request_offer", bad)
    monkeypatch.setattr(hs, "get_order", lambda oid: {"order_id": oid, "status": "pending_approval",
                                                       "approval": {"approval_url": "x", "token": "y",
                                                                    "status": "pending"}})

    async def go():
        async with Client(build_server(hs)) as client:
            r1 = await _call(client, "request_offer", {"delivery_date": "2026-11-16", "lines": []})
            r2 = await _call(client, "get_order", {"order_id": "ord_1"})
            r3 = await _call(client, "no_such_tool", {})
            return r1, r2, r3

    (offer, offer_err), (order, order_err), (_, unknown_err) = anyio.run(go)
    assert offer_err and offer["error"] == "signature_verification_failed"
    assert "must not be used" in offer["message"]
    assert not order_err and order["approval"] == {"status": "pending"}  # FR-35 scrub
    assert unknown_err


def test_server_module_does_not_import_gateway():
    src = open(mcp_server.__file__, encoding="utf-8").read()
    assert "from handshake" not in src and "import handshake" not in src


# ---------------------------------------------------------------- end to end (database)


@pytest.fixture
def app():
    from handshake import timeutil, upstream
    from handshake.api import create_app

    closing = timeutil.now() + timedelta(days=1)
    upstream.set_adapter(FakeUpstream([
        make_facts("464306", price="1.01", comparison="1.01", amount=None,
                   available_date=delivery(), closing=closing),
    ]))
    yield create_app()
    upstream.set_adapter(None)


def _server_for(app, party):
    hs = HandshakeClient("http://testserver", party.key, http=TestClient(app))
    return build_server(hs), hs


OFFER_ARGS = lambda: {  # noqa: E731
    "delivery_date": delivery().isoformat(),
    "lines": [{"product_ids": ["464306"], "quantity": {"value": 1000, "unit": "L"},
               "requirements": {"required_claims": ["FREE_FROM_LACTOSE"]}}],
}


async def _order_until_placed(client: Client) -> tuple[dict, dict, dict]:
    mandate, err = await _call(client, "get_mandate")
    assert not err, mandate
    search, err = await _call(client, "search_products",
                              {"category": "10400", "claims": ["FREE_FROM_LACTOSE"]})
    assert not err and any(i["product_id"] == "464306" for i in search["items"])
    assert all("untrusted_text" not in i for i in search["items"])
    product, err = await _call(client, "get_product", {"product_id": "464306"})
    assert not err and "untrusted_text" not in product
    offer, err = await _call(client, "request_offer", OFFER_ARGS())
    assert not err, offer
    opt = offer["options"][0]
    assert opt["signature"]["verified"] is True and "jws" not in opt
    offer_id = opt["offer"]["offer_id"]
    dry, err = await _call(client, "dry_run_order", {"offer_id": offer_id})
    assert not err, dry
    order, err = await _call(client, "place_order", {"offer_id": offer_id, "reference": "PO-MCP"})
    assert not err, order
    again, err = await _call(client, "place_order", {"offer_id": offer_id, "reference": "PO-MCP"})
    assert not err and again["order_id"] == order["order_id"]  # idempotent retry
    return mandate, dry, order


@pytest.mark.db
def test_full_order_accepted(app, catalog, clean_db):
    party = make_party(limits={"max_order_total_excl_tax": "5000.00"})
    server, hs = _server_for(app, party)

    async def go():
        async with Client(server) as client:
            _, dry, order = await _order_until_placed(client)
            receipt, err = await _call(client, "get_receipt", {"order_id": order["order_id"]})
            return dry, order, receipt, err

    dry, order, receipt, err = anyio.run(go)
    assert dry["outcome"] == "accepted"
    assert order["status"] == "confirmed"
    assert not err and receipt["receipt"]["order_id"] == order["order_id"]
    assert "offer_jws" not in receipt["receipt"]


@pytest.mark.db
def test_full_order_needs_approval_then_confirmed(app, catalog, clean_db):
    from handshake import db, orders

    party = make_party(limits={"max_order_total_excl_tax": "1000.00"})
    server, hs = _server_for(app, party)

    async def go():
        async with Client(server) as client:
            _, dry, order = await _order_until_placed(client)
            pending, err = await _call(client, "get_order", {"order_id": order["order_id"]})
            assert not err
            with db.connection() as conn:  # the human approves out of band
                approval = orders.load_approval(conn, order["order_id"])
            orders.decide_approval(orders.approval_token(approval["approval_id"]), "approve", None)
            final, err = await _call(client, "get_order", {"order_id": order["order_id"]})
            assert not err
            receipt, err = await _call(client, "get_receipt", {"order_id": order["order_id"]})
            return dry, order, pending, final, receipt, err

    dry, order, pending, final, receipt, err = anyio.run(go)
    assert dry["outcome"] == "needs_approval"
    assert order["status"] == "pending_approval" and pending["status"] == "pending_approval"
    assert final["status"] == "confirmed"
    assert not err and receipt["receipt"]["order_id"] == order["order_id"]
    text = json.dumps([order, pending, final])
    assert "/approve/" not in text and "approval_url" not in text  # FR-35


@pytest.mark.db
def test_api_error_is_tool_error(app, catalog, clean_db):
    party = make_party(limits={"max_order_total_excl_tax": "5000.00"})
    server, hs = _server_for(app, party)

    async def go():
        async with Client(server) as client:
            r1 = await _call(client, "get_order", {"order_id": "ord_doesnotexist"})
            r2 = await _call(client, "dry_run_order", {"offer_id": "off_doesnotexist"})
            return r1, r2

    (missing, err1), (dry, err2) = anyio.run(go)
    for res, err in ((missing, err1), (dry, err2)):
        assert err is True
        assert res["error"] == "gateway_error" and res["status"] >= 400 and res["code"]
        assert {"detail", "next", "excluded"} <= set(res)
