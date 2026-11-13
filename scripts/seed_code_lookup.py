"""Seed `code_lookup` with the team's inferred code names (task T-007; document 06, 3.3; A-06).

Every row is stored with verified = FALSE until Valio Aimo confirms it (Q-07).
Idempotent: replaces existing rows with the same (kind, code).

    python scripts/seed_code_lookup.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from handshake import db  # noqa: E402

# (kind, code, inferred English name). Source: document 06, section 3.3.
ENTRIES: list[tuple[str, str, str]] = [
    ("category", "10400", "Milk and milk drinks"),
    ("category", "10411", "Fermented milk (viili)"),
    ("category", "10430", "Quark"),
    ("unit", "ST", "Piece"),
    ("unit", "KI", "Case"),
    ("unit", "PAL", "Pallet"),
    ("unit", "KRS", "Pallet layer"),
    ("unit", "PAK", "Pack"),
    ("unit", "PRK", "Tub or jar"),
    ("unit", "RAS", "Box"),
    ("unit", "BOT", "Bottle"),
    ("unit", "BAG", "Bag"),
    ("unit", "KG", "Kilogram"),
    ("allergen", "AM", "Milk"),
    ("allergen", "ML", "Lactose"),
    ("allergen", "AW", "Cereals containing gluten"),
    ("allergen", "UW", "Wheat"),
    ("allergen", "AE", "Eggs"),
    ("allergen", "AN", "Tree nuts"),
    ("allergen", "AP", "Peanuts"),
    ("allergen", "AY", "Soybeans"),
    ("allergen", "AF", "Fish"),
    ("allergen", "AC", "Crustaceans"),
    ("allergen", "UM", "Molluscs"),
    ("allergen", "AS", "Sesame"),
    ("allergen", "BM", "Mustard"),
    ("allergen", "BC", "Celery"),
    ("allergen", "AU", "Sulphites"),
    ("allergen", "NL", "Lupin"),
]


def seed(conn) -> int:  # noqa: ANN001
    for kind, code, name in ENTRIES:
        db.q(
            conn,
            "REPLACE INTO code_lookup (kind, code, name_en, verified) "
            "VALUES (:kind, :code, :name, FALSE)",
            kind=kind, code=code, name=name,
        )
    return len(ENTRIES)


def main() -> int:
    with db.transaction() as conn:
        n = seed(conn)
    print(f"code_lookup: {n} rows seeded (verified = FALSE)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
