"""Reset the demo (task T-306; document 09, section 6: "Run reset_demo.py first").

In one transaction: delete all trading rows (offers, holds, orders, approvals, idempotency
records, events, upstream snapshots; `handshake.demo.reset`), then re-run the demo seed so the
demo customers, agents and mandates are back to the document 09 values. Agent keys are not
rotated, so keys/demo_agents.json stays valid. Products and the catalog are kept.

    python scripts/reset_demo.py [--database-url URL] [--force]

Refuses to run against a database whose name contains "test" unless --force is given (the
test suite owns those databases). A running gateway keeps its in-process upstream cache; the
cache holds only short-lived upstream answers and is not trading state.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from handshake import db, demo  # noqa: E402
from handshake.config import get_settings  # noqa: E402


def database_name(url: str) -> str:
    """Database name from a SQLAlchemy URL (the last path segment, without query)."""
    return url.rsplit("/", 1)[-1].split("?", 1)[0] if url else ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--database-url", help="override DATABASE_URL")
    ap.add_argument("--force", action="store_true",
                    help="allow a database whose name contains 'test'")
    args = ap.parse_args(argv)

    url = args.database_url or get_settings().database_url
    if not url:
        print("DATABASE_URL is not set; pass --database-url.", file=sys.stderr)
        return 2
    name = database_name(url)
    if "test" in name.lower() and not args.force:
        print(f"Refusing to reset database {name!r}: its name contains 'test'. "
              "Use --force if you really mean it.", file=sys.stderr)
        return 2

    import seed_demo  # noqa: PLC0415 - after sys.path setup and the safety check

    if args.database_url:
        db.set_engine(db.make_engine(args.database_url))
    with db.transaction() as conn:
        deleted = demo.reset(conn)
        seeded = seed_demo.seed(conn)

    print(f"Demo reset on database {name!r}.")
    print("Deleted rows:")
    width = max(len(t) for t in deleted)
    for table, n in deleted.items():
        print(f"  {table.ljust(width)}  {n}")
    print(f"  {'total'.ljust(width)}  {sum(deleted.values())}")
    print("Kept: products and catalog tables, customers, agents, mandates.")
    for slug, r in seeded.items():
        agent_name = seed_demo.DEMO[slug][1]
        state = "restored" if r["mandate_changed"] else "unchanged"
        print(f"{agent_name}: agent {r['agent_id']}, mandate {r['mandate_id']} "
              f"v{r['mandate_version']} ({state})")
    if any(r["key"] for r in seeded.values()):
        seed_demo._write_keys(seeded)  # a missing demo agent was created with a new key
        print(f"New agent keys written to {seed_demo._shown(seed_demo.KEYS_FILE)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
