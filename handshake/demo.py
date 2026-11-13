"""Demo reset (task T-306; document 03, section 7; document 09, section 6).

`reset(conn)` deletes every trading row so the demo can be run again from a clean state:
offers, offer lines, stock holds, orders, approvals, idempotency records, events and the
upstream snapshots (price quotes, availability snapshots). Products, the catalog tables,
customers, agents and mandates are kept. System-versioned tables (stock_holds) keep their
history rows; only current rows are deleted.

Runs inside the caller's transaction and does not commit. Used by `POST /v1/admin/reset`
(DEMO_MODE only) and by `scripts/reset_demo.py`.
"""

from __future__ import annotations

from sqlalchemy import Connection

from . import db

#: Deletion order respects the foreign keys: approvals -> orders -> offers, offer_lines ->
#: offers. The other tables have no foreign keys to trading rows.
RESET_TABLES: tuple[str, ...] = (
    "idempotency_keys",
    "approvals",
    "orders",
    "stock_holds",
    "offer_lines",
    "offers",
    "events",
    "price_quotes",
    "availability_snapshots",
)

#: Tables a reset never touches.
KEPT_TABLES: tuple[str, ...] = (
    "products", "product_claims", "product_allergens", "product_labels",
    "product_substitutions", "code_lookup", "customers", "agents", "mandates",
)


def reset(conn: Connection) -> dict[str, int]:
    """Delete all trading rows in the caller's transaction. Returns deleted rows per table."""
    deleted: dict[str, int] = {}
    for table in RESET_TABLES:
        result = db.q(conn, f"DELETE FROM `{table}`")  # noqa: S608 - fixed table names
        deleted[table] = int(result.rowcount or 0)
    return deleted


__all__ = ["KEPT_TABLES", "RESET_TABLES", "reset"]
