"""Demo reset (task T-306; document 03, section 7; document 09, section 6)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from handshake import db, demo, mandates, timeutil, upstream

from .conftest import CATALOG_TABLES, PARTY_TABLES, TRADING_TABLES
from .factories import make_party
from .fakes import FakeUpstream, make_facts

pytestmark = [pytest.mark.db]


def delivery():
    d = timeutil.today_helsinki() + timedelta(days=2)
    while d.isoweekday() == 7:
        d += timedelta(days=1)
    return d


def count(table: str) -> int:
    with db.connection() as conn:
        return db.q(conn, f"SELECT COUNT(*) FROM `{table}`").scalar_one()


def _snapshots() -> None:
    """FakeUpstream does not write upstream snapshots; insert one of each directly."""
    now = timeutil.to_db(timeutil.now())
    with db.transaction() as conn:
        db.q(conn, "INSERT INTO price_quotes (product_id, pricing_date, fetched_at, currency, "
                   "net_price_found, error, raw_json) VALUES ('464306', :d, :t, 'EUR', 1, 0, "
                   "'{}')", d=delivery(), t=now)
        db.q(conn, "INSERT INTO availability_snapshots (product_id, requested_date, fetched_at, "
                   "sales_block, raw_json) VALUES ('464306', :d, :t, 0, '{}')",
             d=delivery(), t=now)


@pytest.fixture
def client():
    from handshake.api import create_app

    upstream.set_adapter(FakeUpstream([make_facts(
        "464306", price="1.01", comparison="1.01", amount=None, available_date=delivery(),
        closing=timeutil.now() + timedelta(days=1))]))
    yield TestClient(create_app())
    upstream.set_adapter(None)


def _trade(client, party) -> None:
    r = client.post("/v1/offers", headers=party.headers, json={
        "delivery_date": delivery().isoformat(),
        "lines": [{"product_ids": ["464306"], "quantity": {"value": 1000, "unit": "L"}}]})
    assert r.status_code == 201, r.json()
    offer_id = r.json()["options"][0]["offer"]["offer_id"]
    r = client.post("/v1/orders", headers={**party.headers, "Idempotency-Key": "reset-0001"},
                    json={"offer_id": offer_id})
    assert r.status_code == 202, r.json()  # cap 1000.00 -> pending approval, approval row


def test_reset_empties_trading_tables_keeps_parties(client, catalog, clean_db):
    p = make_party()
    _trade(client, p)
    _snapshots()
    for t in TRADING_TABLES:
        assert count(t) > 0, t
    kept = {t: count(t) for t in PARTY_TABLES + CATALOG_TABLES}

    with db.transaction() as conn:
        deleted = demo.reset(conn)

    assert set(deleted) == set(TRADING_TABLES)
    assert all(n > 0 for n in deleted.values()), deleted
    for t in TRADING_TABLES:
        assert count(t) == 0, t
    for t, n in kept.items():
        assert count(t) == n, t
    # The agent can trade again straight away with its mandate intact.
    r = client.get("/v1/mandates/current", headers=p.headers)
    assert r.status_code == 200 and r.json()["mandate_id"] == p.mandate.mandate_id
    _trade(client, p)


def test_reset_rolls_back_with_caller_transaction(client, catalog, clean_db):
    _trade(client, make_party())
    before = count("offers")

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with db.transaction() as conn:
            demo.reset(conn)
            raise Boom
    assert count("offers") == before


def test_reset_on_empty_database(clean_db):
    with db.transaction() as conn:
        assert demo.reset(conn) == {t: 0 for t in demo.RESET_TABLES}


# ---------------------------------------------------------------- scripts/reset_demo.py


def _script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "reset_demo.py"
    spec = importlib.util.spec_from_file_location("reset_demo", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_reset_script_refuses_test_database(test_engine, capsys):
    mod = _script()
    url = test_engine.url.render_as_string(hide_password=False)
    assert mod.main(["--database-url", url]) == 2
    assert "Refusing" in capsys.readouterr().err


def test_reset_script_force_resets_and_reseeds(client, catalog, clean_db, test_engine, capsys,
                                               monkeypatch, tmp_path):
    import sys

    mod = _script()
    import seed_demo  # on sys.path via the script

    monkeypatch.setattr(seed_demo, "KEYS_FILE", tmp_path / "demo_agents.json")
    _trade(client, make_party())
    url = test_engine.url.render_as_string(hide_password=False)
    try:
        assert mod.main(["--database-url", url, "--force"]) == 0
    finally:
        db.set_engine(test_engine)  # main() pointed the module at a new engine
    out = capsys.readouterr().out
    assert "Deleted rows:" in out and "offers" in out
    for t in TRADING_TABLES:
        assert count(t) == 0, t
    with db.connection() as conn:
        agents = dict(db.q(conn, "SELECT name, agent_id FROM agents").all())
        assert {"Nordlys purchasing agent", "Koivula purchasing agent"} <= set(agents)
        m = mandates.get_current(conn, agents["Nordlys purchasing agent"])
    assert str(m.limits.max_order_total_excl_tax) == "1000.00"
    assert "seed_demo" in sys.modules
