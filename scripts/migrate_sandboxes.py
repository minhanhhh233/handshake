"""Add the public demo sandbox schema to an existing database (stage 1, 2026-11-14).

Idempotent: adds customers.sandbox_id / sandbox_persona and the sandboxes table if missing.
Uses DATABASE_URL like every script (document 02, section 7).

    python scripts/migrate_sandboxes.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from handshake import db  # noqa: E402


def main() -> int:
    with db.transaction() as conn:
        cols = {r[0] for r in db.q(conn, "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                                         "WHERE TABLE_SCHEMA = DATABASE() AND "
                                         "TABLE_NAME = 'customers'").all()}
        if "sandbox_id" not in cols:
            db.q(conn, "ALTER TABLE customers ADD COLUMN sandbox_id VARCHAR(32) NULL, "
                       "ADD COLUMN sandbox_persona VARCHAR(32) NULL, "
                       "ADD KEY ix_customers_sandbox (sandbox_id, sandbox_persona)")
            print("customers: sandbox columns added")
        else:
            print("customers: already migrated")
        db.q(conn, "CREATE TABLE IF NOT EXISTS sandboxes ("
                   "sandbox_id VARCHAR(32) NOT NULL PRIMARY KEY, ip_hash VARCHAR(32) NOT NULL, "
                   "created_at DATETIME(3) NOT NULL, expires_at DATETIME(3) NOT NULL, "
                   "KEY ix_sandboxes_ip (ip_hash, created_at)) "
                   "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
        print("sandboxes: table present")
        db.q(conn, "CREATE TABLE IF NOT EXISTS sandbox_overlays ("
                   "sandbox_id VARCHAR(32) NOT NULL, product_id VARCHAR(16) NOT NULL, "
                   "text_en VARCHAR(500) NOT NULL, created_at DATETIME(3) NOT NULL, "
                   "PRIMARY KEY (sandbox_id, product_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
        print("sandbox_overlays: table present")
        db.q(conn, "CREATE TABLE IF NOT EXISTS proposals ("
                   "proposal_id VARCHAR(32) NOT NULL PRIMARY KEY, agent_id VARCHAR(32) NOT NULL, "
                   "customer_id VARCHAR(32) NOT NULL, offer_id VARCHAR(32) NULL, "
                   "product_id VARCHAR(16) NOT NULL, status ENUM('open','accepted','declined',"
                   "'expired','blocked') NOT NULL, reason_untrusted VARCHAR(500) NULL, "
                   "blocked_reason VARCHAR(500) NULL, trace_id VARCHAR(32) NOT NULL, "
                   "created_at DATETIME(3) NOT NULL, expires_at DATETIME(3) NULL, "
                   "decided_at DATETIME(3) NULL, KEY ix_proposal_agent (agent_id, created_at), "
                   "KEY ix_proposal_offer (offer_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
        print("proposals: table present")
        ocols = {r[0] for r in db.q(conn, "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                                          "WHERE TABLE_SCHEMA = DATABASE() AND "
                                          "TABLE_NAME = 'orders'").all()}
        if "rationale_untrusted" not in ocols:
            db.q(conn, "ALTER TABLE orders ADD COLUMN rationale_untrusted VARCHAR(500) NULL "
                       "AFTER note_untrusted")
            print("orders: rationale_untrusted added (stage 3)")
        else:
            print("orders: rationale already present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
