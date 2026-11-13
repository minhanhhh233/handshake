"""Tests for shared infrastructure: event chain, mandates, authentication, signer core."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from handshake import db, events, mandates, signing
from handshake.api.deps import AgentContext, optional_agent, require_admin, require_agent
from handshake.errors import HandshakeError
from handshake.ids import new_id

from .factories import make_party, mandate_body

pytestmark = pytest.mark.db


def test_s04_chain_of_five_verifies_and_tampering_is_located(clean_db):
    trace = new_id("trc")
    with db.transaction() as conn:
        for i in range(5):
            events.append(conn, trace_id=trace, type="test.step", actor_type="gateway",
                          text=f"step {i}", body={"i": i, "amount": "10.50"})
    with db.connection() as conn:
        evs = events.list_events(conn, trace)
    assert [e["seq"] for e in evs] == [1, 2, 3, 4, 5]
    assert evs[0]["prev_hash"] == events.GENESIS
    assert events.verify_chain(evs) == (True, None)
    evs[2]["body"] = {"i": 2, "amount": "99.99"}
    assert events.verify_chain(evs) == (False, 3)


def test_buyer_view_omits_aimo_only_events(clean_db):
    trace = new_id("trc")
    with db.transaction() as conn:
        events.append(conn, trace_id=trace, type="a", actor_type="agent", text="x")
        events.append(conn, trace_id=trace, type="b", actor_type="gateway", text="y",
                      audience="aimo_only")
    with db.connection() as conn:
        assert [e["type"] for e in events.list_events(conn, trace, audience="both")] == ["a"]
        assert len(events.list_events(conn, trace)) == 2


def test_mandate_replace_increments_version_and_keeps_id(clean_db):
    p = make_party()
    assert p.mandate.version == 1
    with db.transaction() as conn:
        m2 = mandates.replace(conn, p.agent_id, p.customer_id,
                              mandate_body(limits={"max_order_total_excl_tax": "2000.00"}))
    assert m2.version == 2 and m2.mandate_id == p.mandate.mandate_id
    with db.connection() as conn:
        cur = mandates.get_current(conn, p.agent_id)
    assert cur.limits.max_order_total_excl_tax == "2000.00"


def _auth_app() -> FastAPI:
    from handshake.api import create_app

    app = create_app()

    @app.get("/_t/agent")
    def a(agent: AgentContext = Depends(require_agent)):  # noqa: B008
        return {"agent_id": agent.agent_id}

    @app.get("/_t/optional")
    def o(agent: AgentContext | None = Depends(optional_agent)):  # noqa: B008
        return {"anonymous": agent is None}

    @app.get("/_t/admin", dependencies=[Depends(require_admin)])
    def ad():
        return {"ok": True}

    return app


def test_authentication_rules(clean_db, monkeypatch):
    p = make_party()
    c = TestClient(_auth_app())
    assert c.get("/_t/agent").json()["code"] == "unauthenticated"
    assert c.get("/_t/agent", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/_t/agent", headers=p.headers).json() == {"agent_id": p.agent_id}
    assert c.get("/_t/optional").json() == {"anonymous": True}
    assert c.get("/_t/optional", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = c.get("/_t/admin")
    assert r.status_code == 403 and r.json()["code"] == "forbidden"


def test_signer_round_trip():
    payload = {"offer_id": "off_x", "totals": {"excl_tax": "1010.00"}, "b": 1, "a": "ä"}
    token = signing.sign(payload, signing.TYP_OFFER)
    assert signing.verify(token, signing.jwks(), signing.TYP_OFFER) == payload
    with pytest.raises(signing.SignatureError):
        signing.verify(token, signing.jwks(), signing.TYP_RECEIPT)


def test_handshake_error_is_exception():
    assert issubclass(HandshakeError, Exception)
