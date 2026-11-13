"""Seed the demo customers, agents and mandates (task T-207; document 09, section 5).

Idempotent. Customers and agents are found by name and reused; a mandate is replaced only when
its content differs, so re-running does not bump versions. Agent keys are generated only when an
agent is created or with --rotate-keys. The database stores only SHA-256 hashes; clear keys go
to the git-ignored file keys/demo_agents.json and are printed only with --show-keys.

    python scripts/seed_demo.py [--rotate-keys] [--show-keys] [--database-url URL]

All names are fictional; contacts use example.com.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from handshake import db, mandates, timeutil  # noqa: E402
from handshake.api.deps import key_hash  # noqa: E402
from handshake.ids import new_id  # noqa: E402
from handshake.models import MandateBody  # noqa: E402

import seed_code_lookup  # noqa: E402

KEYS_FILE = ROOT / "keys" / "demo_agents.json"

_BASE: dict[str, Any] = {
    "valid_from": "2026-11-01T00:00:00+02:00",
    "valid_to": None,
    "limits": {"currency": "EUR", "max_order_total_excl_tax": None,
               "max_weekly_total_excl_tax": None},
    "allowed_categories": None,
    "forbidden_allergens": {"codes": [], "states": ["CONTAINS", "MAY_CONTAIN"],
                            "unknown": "reject"},
    "substitution": {"auto_approve": "official_rules_only"},
    "delivery": {"allowed_weekdays": [1, 2, 3, 4, 5, 6]},
    "stock": {"require_confirmed": False},
    "preferences": {"weights": {"price": 0.6, "delivery_reliability": 0.2, "carbon": 0.1,
                                "labels": 0.1},
                    "preferred_labels": []},
    "proactive_offers": {"enabled": False, "categories": []},
}


def _mandate(**overrides: Any) -> MandateBody:
    doc = json.loads(json.dumps(_BASE))
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(doc.get(k), dict):
            doc[k] = {**doc[k], **v}
        else:
            doc[k] = v
    return MandateBody.model_validate(doc)


#: slug -> (customer name, agent name, mandate body)
DEMO: dict[str, tuple[str, str, MandateBody]] = {
    "nordlys": (
        "Nordlys Hotels",
        "Nordlys purchasing agent",
        _mandate(
            approvers=[{"name": "Maria Virtanen", "contact": "maria@example.com"}],
            limits={"max_order_total_excl_tax": "1000.00"},
            preferences={"preferred_labels": ["GOODS_FROM_FINLAND_BLUE_SWAN"]},
        ),
    ),
    "koivula": (
        "Koivula School Kitchens",
        "Koivula purchasing agent",
        _mandate(
            approvers=[{"name": "Antti Laine", "contact": "antti@example.com"}],
            limits={"max_order_total_excl_tax": "500.00"},
            forbidden_allergens={"codes": ["AM"], "states": ["CONTAINS", "MAY_CONTAIN"],
                                 "unknown": "reject"},
        ),
    ),
}


def new_agent_key() -> str:
    return "hsk_" + secrets.token_urlsafe(24)


def _content(doc: dict[str, Any]) -> dict[str, Any]:
    """Mandate document without the gateway-assigned fields, for comparison."""
    return {k: v for k, v in doc.items()
            if k not in ("mandate_id", "version", "agent_id", "customer_id")}


def _customer(conn, name: str, now) -> tuple[str, bool]:  # noqa: ANN001
    row = db.q(conn, "SELECT customer_id FROM customers WHERE name = :n "
                     "ORDER BY created_at, customer_id LIMIT 1", n=name).first()
    if row:
        return row[0], False
    cid = new_id("cus")
    db.q(conn, "INSERT INTO customers (customer_id, name, created_at) VALUES (:c, :n, :t)",
         c=cid, n=name, t=now)
    return cid, True


def _agent(conn, customer_id: str, name: str, now) -> tuple[str, str | None]:  # noqa: ANN001
    """Returns (agent_id, clear key if the agent was created else None)."""
    row = db.q(conn, "SELECT agent_id FROM agents WHERE customer_id = :c AND name = :n "
                     "ORDER BY created_at, agent_id LIMIT 1", c=customer_id, n=name).first()
    if row:
        return row[0], None
    aid, key = new_id("agt"), new_agent_key()
    db.q(conn, "INSERT INTO agents (agent_id, customer_id, name, key_sha256, status, created_at) "
               "VALUES (:a, :c, :n, :h, 'active', :t)",
         a=aid, c=customer_id, n=name, h=key_hash(key), t=now)
    return aid, key


def seed(conn, rotate_keys: bool = False) -> dict[str, dict[str, Any]]:  # noqa: ANN001
    """Seed code_lookup and the demo parties inside the caller's transaction.

    Returns per slug: customer_id, agent_id, key (clear key when created or rotated, else
    None), created (agent created), mandate_id, mandate_version, mandate_changed.
    """
    seed_code_lookup.seed(conn)
    now = timeutil.to_db(timeutil.now())
    out: dict[str, dict[str, Any]] = {}
    for slug, (customer_name, agent_name, body) in DEMO.items():
        customer_id, _ = _customer(conn, customer_name, now)
        agent_id, key = _agent(conn, customer_id, agent_name, now)
        created = key is not None
        if rotate_keys and not created:
            key = new_agent_key()
            db.q(conn, "UPDATE agents SET key_sha256 = :h WHERE agent_id = :a",
                 h=key_hash(key), a=agent_id)
        current = mandates.get_current(conn, agent_id)
        changed = current is None or _content(current.dump()) != body.dump()
        mandate = mandates.replace(conn, agent_id, customer_id, body) if changed else current
        out[slug] = {
            "customer_id": customer_id, "agent_id": agent_id, "key": key, "created": created,
            "mandate_id": mandate.mandate_id, "mandate_version": mandate.version,
            "mandate_changed": changed,
        }
    return out


def _write_keys(result: dict[str, dict[str, Any]]) -> list[str]:
    """Merge new clear keys into keys/demo_agents.json. Returns slugs with no known key."""
    try:
        stored = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    missing = []
    for slug, r in result.items():
        if r["key"]:
            stored[slug] = {"agent_id": r["agent_id"], "key": r["key"]}
        elif stored.get(slug, {}).get("agent_id") != r["agent_id"]:
            stored.pop(slug, None)
            missing.append(slug)
    KEYS_FILE.parent.mkdir(exist_ok=True)
    KEYS_FILE.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")
    return missing


def _shown(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--database-url", help="override DATABASE_URL")
    ap.add_argument("--rotate-keys", action="store_true", help="issue new keys for all agents")
    ap.add_argument("--show-keys", action="store_true", help="print the clear agent keys")
    args = ap.parse_args(argv)

    if args.database_url:
        db.set_engine(db.make_engine(args.database_url))
    with db.transaction() as conn:
        result = seed(conn, rotate_keys=args.rotate_keys)
    missing = _write_keys(result)

    for slug, r in result.items():
        name = DEMO[slug][1]
        agent_state = "created" if r["created"] else ("key rotated" if r["key"] else "kept")
        mandate_state = ("set" if r["mandate_version"] == 1 else "updated")             if r["mandate_changed"] else "unchanged"
        print(f"{name}: agent {r['agent_id']} ({agent_state}), customer {r['customer_id']}, "
              f"mandate {r['mandate_id']} v{r['mandate_version']} ({mandate_state})")
        if args.show_keys and r["key"]:
            print(f"  key: {r['key']}")
    if any(r["key"] for r in result.values()):
        print(f"Agent keys written to {_shown(KEYS_FILE)}"
              + ("" if args.show_keys else " (use --show-keys to print them)"))
    if missing:
        print(f"No stored key for: {', '.join(missing)}. Run with --rotate-keys to issue new ones.")
    if args.show_keys and not any(r["key"] for r in result.values()):
        print(f"No new keys this run; existing keys are in {_shown(KEYS_FILE)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
