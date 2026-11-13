"""GET /v1/mandates/current and admin mandate/events endpoints (T-202; document 03, 4 and 7)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from handshake import db, events
from handshake.config import get_settings
from handshake.ids import new_id

from .factories import make_party, mandate_body

pytestmark = [pytest.mark.db]


@pytest.fixture
def client():
    from handshake.api import create_app

    return TestClient(create_app())


@pytest.fixture
def admin():
    key = get_settings().admin_key
    if not key:
        pytest.skip("ADMIN_KEY not set")
    return {"X-Admin-Key": key}


def body_json(**overrides):
    return mandate_body(**overrides).dump()


def test_current_mandate_hides_contacts(client, clean_db):
    p = make_party()
    r = client.get("/v1/mandates/current", headers=p.headers)
    assert r.status_code == 200
    d = r.json()
    assert d["mandate_id"] == p.mandate.mandate_id and d["version"] == 1
    assert d["approvers"] == [{"name": "Maria Virtanen"}]
    assert "maria@example.com" not in r.text
    assert d["limits"]["max_order_total_excl_tax"] == "1000.00"


def test_current_mandate_requires_key(client, clean_db):
    r = client.get("/v1/mandates/current")
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated"


def test_current_mandate_404_when_none(client, clean_db):
    p = make_party()
    with db.transaction() as conn:
        db.q(conn, "DELETE FROM mandates WHERE agent_id = :a", a=p.agent_id)
    r = client.get("/v1/mandates/current", headers=p.headers)
    assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_admin_put_creates_next_version(client, clean_db, admin):
    p = make_party()
    new = body_json(limits={"max_order_total_excl_tax": "750.00"})
    r = client.put(f"/v1/admin/agents/{p.agent_id}/mandate", json=new, headers=admin)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["version"] == 2 and d["mandate_id"] == p.mandate.mandate_id
    assert d["agent_id"] == p.agent_id and d["customer_id"] == p.customer_id
    assert d["limits"]["max_order_total_excl_tax"] == "750.00"
    r = client.get("/v1/mandates/current", headers=p.headers)
    assert r.json()["version"] == 2
    r = client.put(f"/v1/admin/agents/{p.agent_id}/mandate", json=new, headers=admin)
    assert r.json()["version"] == 3


def test_admin_put_requires_admin_key(client, clean_db):
    p = make_party()
    url = f"/v1/admin/agents/{p.agent_id}/mandate"
    r = client.put(url, json=body_json())
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    r = client.put(url, json=body_json(), headers={"X-Admin-Key": "wrong"})
    assert r.status_code == 403
    r = client.put(url, json=body_json(), headers=p.headers)  # agent key is not admin
    assert r.status_code == 403


def test_admin_put_unknown_agent(client, clean_db, admin):
    r = client.put(f"/v1/admin/agents/{new_id('agt')}/mandate", json=body_json(), headers=admin)
    assert r.status_code == 404 and r.json()["code"] == "not_found"


@pytest.mark.parametrize("mutate", [
    lambda b: b.pop("approvers"),
    lambda b: b.update(approvers=[]),
    lambda b: b.update(mandate_id="mnd_x", version=7),
    lambda b: b["limits"].update(max_order_total_excl_tax="lots"),
    lambda b: b["delivery"].update(allowed_weekdays=[0, 8]),
])
def test_admin_put_invalid_body(client, clean_db, admin, mutate):
    p = make_party()
    b = body_json()
    mutate(b)
    r = client.put(f"/v1/admin/agents/{p.agent_id}/mandate", json=b, headers=admin)
    assert r.status_code == 400 and r.json()["code"] == "invalid_request"
    r = client.get(f"/v1/admin/agents/{p.agent_id}/mandate", headers=admin)
    assert r.json()["version"] == 1


def test_admin_get_includes_contacts(client, clean_db, admin):
    p = make_party()
    r = client.get(f"/v1/admin/agents/{p.agent_id}/mandate", headers=admin)
    assert r.status_code == 200
    assert r.json()["approvers"] == [{"name": "Maria Virtanen", "contact": "maria@example.com"}]
    assert client.get(f"/v1/admin/agents/{p.agent_id}/mandate").status_code == 403
    r = client.get(f"/v1/admin/agents/{new_id('agt')}/mandate", headers=admin)
    assert r.status_code == 404


def test_admin_events_of_trace_in_seq_order(client, clean_db, admin):
    trace, other = new_id("trc"), new_id("trc")
    with db.transaction() as conn:
        for i in range(3):
            events.append(conn, trace_id=trace, type=f"test.step{i}", actor_type="gateway",
                          text=f"step {i}", body={"i": i})
        events.append(conn, trace_id=other, type="test.other", actor_type="agent",
                      actor_id="agt_x", text="other")
    r = client.get("/v1/admin/events", params={"trace_id": trace}, headers=admin)
    assert r.status_code == 200
    evs = r.json()["events"]
    assert [e["seq"] for e in evs] == [1, 2, 3]
    assert {e["trace_id"] for e in evs} == {trace}
    e = evs[0]
    assert set(e) == {"event_id", "trace_id", "seq", "type", "at", "actor", "audience", "text",
                      "body", "hash"}
    assert e["actor"] == {"type": "gateway", "id": None} and e["body"] == {"i": 0}
    assert e["at"][-6:] in ("+02:00", "+03:00") and len(e["hash"]) == 64

    r = client.get("/v1/admin/events", params={"limit": 2}, headers=admin)
    assert len(r.json()["events"]) == 2
    r = client.get("/v1/admin/events", headers=admin)
    assert len(r.json()["events"]) == 4
    assert client.get("/v1/admin/events", params={"limit": 0}, headers=admin).status_code == 400
    assert client.get("/v1/admin/events", params={"limit": 501}, headers=admin).status_code == 400
    assert client.get("/v1/admin/events").status_code == 403
