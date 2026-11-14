"""scripts/seed_demo.py (T-207; document 09, section 5)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from handshake import db, mandates
from handshake.api.deps import key_hash

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import seed_demo  # noqa: E402

pytestmark = [pytest.mark.db]


def run(rotate_keys=False):
    with db.transaction() as conn:
        return seed_demo.seed(conn, rotate_keys=rotate_keys)


def scalar(sql, **kw):
    with db.connection() as conn:
        return db.q(conn, sql, **kw).scalar_one()


def mandate(agent_id):
    with db.connection() as conn:
        return mandates.get_current(conn, agent_id)


def test_seed_is_idempotent(clean_db):
    first = run()
    assert all(r["created"] and r["key"] for r in first.values())
    second = run()
    # Nordlys, Koivula and the public demo customer (two public agents, document 09, 6a).
    assert scalar("SELECT COUNT(*) FROM customers") == 3
    assert scalar("SELECT COUNT(*) FROM agents") == 4
    for slug in ("nordlys", "koivula"):
        assert second[slug]["agent_id"] == first[slug]["agent_id"]
        assert second[slug]["key"] is None and not second[slug]["created"]
        assert not second[slug]["mandate_changed"]
        assert mandate(first[slug]["agent_id"]).version == 1
    assert scalar("SELECT COUNT(*) FROM code_lookup WHERE kind = 'allergen' AND code = 'AM'") == 1
    names = {scalar("SELECT name FROM customers WHERE customer_id = :c", c=r["customer_id"])
             for r in second.values()}
    assert names == {"Nordlys Hotels", "Koivula School Kitchens", "Handshake public demo"}


def test_keys_stored_as_hashes(clean_db):
    r = run()["nordlys"]
    stored = scalar("SELECT key_sha256 FROM agents WHERE agent_id = :a", a=r["agent_id"])
    assert stored == key_hash(r["key"]) and r["key"].startswith("hsk_")


def test_koivula_mandate(clean_db):
    m = mandate(run()["koivula"]["agent_id"])
    assert m.limits.max_order_total_excl_tax == "500.00"
    assert m.forbidden_allergens.codes == ["AM"]
    assert m.forbidden_allergens.states == ["CONTAINS", "MAY_CONTAIN"]
    assert m.forbidden_allergens.unknown == "reject"
    assert m.approvers[0].name == "Antti Laine"
    assert m.approvers[0].contact.endswith("@example.com")


def test_nordlys_mandate(clean_db):
    m = mandate(run()["nordlys"]["agent_id"])
    assert m.limits.max_order_total_excl_tax == "1000.00"
    assert m.delivery.allowed_weekdays == [1, 2, 3, 4, 5, 6]
    assert m.allowed_categories is None and m.forbidden_allergens.codes == []
    assert m.substitution.auto_approve == "official_rules_only"
    assert m.preferences.preferred_labels == ["GOODS_FROM_FINLAND_BLUE_SWAN"]
    assert m.approvers[0].name == "Maria Virtanen" and m.valid_to is None
    assert m.dump()["valid_from"] == "2026-11-01T00:00:00+02:00"


def test_changed_mandate_is_restored_with_next_version(clean_db):
    agent_id = run()["nordlys"]["agent_id"]
    with db.transaction() as conn:
        cust = scalar("SELECT customer_id FROM agents WHERE agent_id = :a", a=agent_id)
        body = seed_demo.DEMO["nordlys"][2].model_copy(
            update={"limits": seed_demo.DEMO["nordlys"][2].limits.model_copy(
                update={"max_order_total_excl_tax": "5.00"})})
        mandates.replace(conn, agent_id, cust, body)
    r = run()["nordlys"]
    assert r["mandate_changed"] and r["mandate_version"] == 3
    assert mandate(agent_id).limits.max_order_total_excl_tax == "1000.00"


def test_rotate_keys_changes_hashes(clean_db):
    first = run()
    before = {s: scalar("SELECT key_sha256 FROM agents WHERE agent_id = :a", a=r["agent_id"])
              for s, r in first.items()}
    rotated = run(rotate_keys=True)
    for slug, r in rotated.items():
        after = scalar("SELECT key_sha256 FROM agents WHERE agent_id = :a", a=r["agent_id"])
        assert after != before[slug] and after == key_hash(r["key"])
        assert r["agent_id"] == first[slug]["agent_id"] and r["mandate_version"] == 1
