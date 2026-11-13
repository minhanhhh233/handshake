"""Load the three challenge files into MariaDB (task T-004; documents 04 section 5, 06 section 8).

    python scripts/load_data.py --products data/products.json \
        --reliability data/reliability.csv --carbon data/carbon.csv
    python scripts/load_data.py --products data/products.json --inspect   # print one record

Idempotent: truncates the catalog tables first. Prints row counts against the expected counts.
The files live in data/, which is git-ignored (NFR-08).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ijson  # noqa: E402

from handshake import db  # noqa: E402

BATCH = 500
EXPECTED = {
    "products": 17546,
    "substitution_rules": 250,
    "with_reliability": 4425,
    "with_carbon": 11033,
    "with_allergen_data": 9436,
}
NOT_LABELS = {"warmTemperature", "coolTemperature", "freezeTemperature"}
STATE_RANK = {"CONTAINS": 3, "MAY_CONTAIN": 2, "FREE_FROM": 1}
UNTRUSTED_FIELDS = {
    "marketingTexts": "marketing",
    "keyIngredients": "key_ingredients",
    "preparationInstructions": "preparation_instructions",
    "usageInstructions": "usage_instructions",
    "storageInstructions": "storage_instructions",
}
CATALOG_TABLES = ["product_claims", "product_allergens", "product_labels",
                  "product_substitutions", "products"]


# ------------------------------------------------------------------ helpers


def get(d: Any, *path: str, default: Any = None) -> Any:
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return default
        d = d[p]
    return d


def first(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).replace(",", "."))
    except InvalidOperation:
        return None


def text_value(item: Any) -> str | None:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return first(item, "value", "name", "text", "description")
    return None


def by_language(items: Any) -> dict[str, str]:
    """[{language: en, value: ...}, ...] -> {en: ...}. Also accepts {en: ...} as is."""
    out: dict[str, str] = {}
    if isinstance(items, dict):
        return {str(k).lower(): str(v) for k, v in items.items() if isinstance(v, str)}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        lang = str(first(it, "language", "lang", "languageCode", default="")).lower()[:2]
        val = text_value(it)
        if lang and val:
            out.setdefault(lang, val)
    return out


def classification(rec: dict, name: str) -> list[dict]:
    groups = rec.get("classifications") or (rec.get("synkkaData") or {}).get("classifications")
    for c in groups or []:
        if isinstance(c, dict) and c.get("name") == name:
            return [v for v in (c.get("values") or []) if isinstance(v, dict)]
    return []


def to_litres(value: Decimal, unit: str | None) -> Decimal | None:
    u = (unit or "").upper()
    return {"MLT": value / 1000, "CLT": value / 100, "DLT": value / 10, "LTR": value,
            "L": value}.get(u)


def to_kg(value: Decimal, unit: str | None) -> Decimal | None:
    u = (unit or "").upper()
    return {"GRM": value / 1000, "KGM": value, "KG": value, "MGM": value / 1_000_000}.get(u)


def unit_size(units: list[dict], unit_code: str) -> Decimal | None:
    for u in units:
        if str(first(u, "unitId", "unit", "unitCode", "code", default="")).upper() == unit_code.upper():
            return dec(first(u, "sizeInBaseUnits", "size_in_base_units", "quantity",
                             "baseUnitQuantity"))
    return None


def conversions(synkka: dict) -> dict[str, dict[str, Decimal]]:
    """{level: {"L": litres, "KG": kg}} from synkkaData.unitConversions."""
    out: dict[str, dict[str, Decimal]] = {}
    raw = synkka.get("unitConversions") or []
    if isinstance(raw, dict):
        raw = [dict(v, level=k) if isinstance(v, dict) else {"level": k} for k, v in raw.items()]
    for c in raw:
        if not isinstance(c, dict):
            continue
        level = str(first(c, "synkkaPackagingType", "level", "type", default="")).upper()
        slot = out.setdefault(level, {})
        for key in ("volume", "netVolume", "netContent"):
            v = c.get(key)
            if isinstance(v, dict):
                lit = to_litres(dec(first(v, "value", "amount")) or Decimal(0),
                                first(v, "unit", "unitCode"))
                if lit:
                    slot.setdefault("L", lit)
        for key in ("netWeight", "weight"):
            v = c.get(key)
            if isinstance(v, dict):
                kg = to_kg(dec(first(v, "value", "amount")) or Decimal(0),
                           first(v, "unit", "unitCode"))
                if kg:
                    slot.setdefault("KG", kg)
    return out


def per_sales_unit(rec: dict, synkka: dict, units: list[dict], kind: str) -> Decimal | None:
    """Document 06, section 5: loader derivation for method `product_data`."""
    conv = conversions(synkka)
    sales_unit = str(rec.get("salesUnit") or "").upper()
    base_unit = str(rec.get("baseUnit") or "").upper()
    base = conv.get("BASE_UNIT_OR_EACH", {}).get(kind)
    if sales_unit and sales_unit == base_unit:
        return base
    if sales_unit == "KI":
        case = conv.get("CASE", {}).get(kind)
        if case:
            return case
        size = unit_size(units, "KI")
        if base and size:
            return base * size
    return None


# ------------------------------------------------------------------ parsing


def iter_products(path: str) -> Iterator[dict]:
    with open(path, "rb") as f:
        head = f.read(4096).lstrip()
    prefix = "item"
    if head[:1] == b"{":
        prefix = "products.item"
        for key in (b'"products"', b'"items"', b'"data"'):
            if key in head:
                prefix = key.strip(b'"').decode() + ".item"
                break
    with open(path, "rb") as f:
        yield from ijson.items(f, prefix, use_float=False)


def transform(rec: dict) -> dict[str, Any]:
    synkka = rec.get("synkkaData") or {}
    names = by_language(synkka.get("names") or rec.get("names"))
    units = rec.get("units") or synkka.get("units") or []
    if isinstance(units, dict):
        units = [dict(v, unit=k) for k, v in units.items() if isinstance(v, dict)]

    claims = sorted({str(v.get("id")) for v in classification(rec, "nutritionalClaim")
                     if v.get("id")})
    labels = sorted({str(v.get("id")) for v in classification(rec, "packagingMarkedLabel")
                     if v.get("id") and v.get("id") not in NOT_LABELS})
    allergens: dict[str, str] = {}
    for v in classification(rec, "allergen"):
        code, state = v.get("id"), str(v.get("unit") or "").upper()
        if code and state in ("CONTAINS", "MAY_CONTAIN"):
            if STATE_RANK[state] > STATE_RANK.get(allergens.get(code, ""), 0):
                allergens[code] = state
    for v in classification(rec, "nonAllergen"):
        code = v.get("id")
        if code and code not in allergens:
            allergens[code] = "FREE_FROM"
    has_allergen_data = bool(classification(rec, "allergen") or
                             classification(rec, "nonAllergen"))

    subs = []
    for s in rec.get("substitutions") or []:
        sid = s if isinstance(s, (str, int)) else first(s, "substituteProduct", "productId", "id")
        if sid is not None:
            subs.append(str(sid))

    untrusted: dict[str, Any] = {}
    for src, dst in UNTRUSTED_FIELDS.items():
        val = rec.get(src, synkka.get(src))
        if val:
            texts = by_language(val) if not isinstance(val, str) else {"fi": val}
            if texts:
                untrusted[dst] = texts

    country = rec.get("countryOfOrigin")
    return {
        "row": {
            "product_id": str(rec["id"]),
            "sales_unit_gtin": rec.get("salesUnitGtin"),
            "consumer_gtin": synkka.get("gtin"),
            "name_en": (names.get("en") or "")[:255] or None,
            "name_fi": (names.get("fi") or "")[:255] or None,
            "brand": (synkka.get("brand") or None),
            "vendor_name": rec.get("vendorName") or rec.get("vendor") or "",
            "category_code": str(rec.get("category") or ""),
            "sales_unit": str(rec.get("salesUnit") or ""),
            "base_unit": str(rec.get("baseUnit") or ""),
            "allowed_lot_size": dec(rec.get("allowedLotSize")) or Decimal("0.01"),
            "variable_unit": bool(synkka.get("variableUnit") or False),
            "country_of_origin": str(country).lower()[:2] if country else None,
            "temperature_condition": rec.get("temperatureCondition"),
            "has_allergen_data": has_allergen_data,
            "net_volume_l_per_sales_unit": per_sales_unit(rec, synkka, units, "L"),
            "net_weight_kg_per_sales_unit": per_sales_unit(rec, synkka, units, "KG"),
            "units_json": json.dumps(units, default=str, ensure_ascii=False),
            # Raw synkkaData.nutritionalContent and synkkaData.ingredients; the catalog
            # shapes them for the API (document 03, section 3).
            "nutrition_json": json.dumps(synkka["nutritionalContent"], default=str,
                                         ensure_ascii=False)
            if synkka.get("nutritionalContent") else None,
            "ingredient_origins_json": json.dumps(synkka["ingredients"], default=str,
                                                  ensure_ascii=False)
            if synkka.get("ingredients") else None,
            "untrusted_text_json": json.dumps(untrusted, ensure_ascii=False)
            if untrusted else None,
        },
        "claims": claims,
        "labels": labels,
        "allergens": allergens,
        "substitutions": list(dict.fromkeys(subs)),
    }


# ------------------------------------------------------------------ database

INSERT_PRODUCT = """
INSERT INTO products (product_id, sales_unit_gtin, consumer_gtin, name_en, name_fi, brand,
  vendor_name, category_code, sales_unit, base_unit, allowed_lot_size, variable_unit,
  country_of_origin, temperature_condition, has_allergen_data, net_volume_l_per_sales_unit,
  net_weight_kg_per_sales_unit, units_json, nutrition_json, ingredient_origins_json,
  untrusted_text_json)
VALUES (:product_id, :sales_unit_gtin, :consumer_gtin, :name_en, :name_fi, :brand,
  :vendor_name, :category_code, :sales_unit, :base_unit, :allowed_lot_size, :variable_unit,
  :country_of_origin, :temperature_condition, :has_allergen_data,
  :net_volume_l_per_sales_unit, :net_weight_kg_per_sales_unit, :units_json, :nutrition_json,
  :ingredient_origins_json, :untrusted_text_json)
"""


def flush(batch: list[dict]) -> None:
    if not batch:
        return
    with db.transaction() as conn:
        conn.execute(db.text(INSERT_PRODUCT), [b["row"] for b in batch])
        claims = [{"p": b["row"]["product_id"], "c": c} for b in batch for c in b["claims"]]
        labels = [{"p": b["row"]["product_id"], "l": lab} for b in batch for lab in b["labels"]]
        allergens = [{"p": b["row"]["product_id"], "a": a, "s": s}
                     for b in batch for a, s in b["allergens"].items()]
        subs = [{"p": b["row"]["product_id"], "s": s} for b in batch for s in b["substitutions"]]
        if claims:
            conn.execute(db.text("INSERT INTO product_claims VALUES (:p, :c)"), claims)
        if labels:
            conn.execute(db.text("INSERT INTO product_labels VALUES (:p, :l)"), labels)
        if allergens:
            conn.execute(db.text("INSERT INTO product_allergens VALUES (:p, :a, :s)"), allergens)
        if subs:
            conn.execute(db.text("INSERT INTO product_substitutions VALUES (:p, :s)"), subs)


def read_csv(path: str, value_col: str) -> dict[str, Decimal]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        out: dict[str, Decimal] = {}
        for row in csv.DictReader(f, dialect=dialect):
            row = {k.strip(): v for k, v in row.items() if k}
            pid, val = row.get("productId"), dec(row.get(value_col))
            if pid and val is not None:
                out[str(pid).strip()] = val
    return out


def update_column(column: str, values: dict[str, Decimal]) -> None:
    rows = [{"p": k, "v": v} for k, v in values.items()]
    with db.transaction() as conn:
        for i in range(0, len(rows), BATCH):
            conn.execute(db.text(f"UPDATE products SET {column} = :v WHERE product_id = :p"),
                         rows[i:i + BATCH])


def counts() -> dict[str, int]:
    with db.connection() as conn:
        one = lambda sql: int(db.q(conn, sql).scalar_one())  # noqa: E731
        return {
            "products": one("SELECT COUNT(*) FROM products"),
            "substitution_rules": one("SELECT COUNT(*) FROM product_substitutions"),
            "with_reliability": one(
                "SELECT COUNT(*) FROM products WHERE delivery_reliability IS NOT NULL"),
            "with_carbon": one(
                "SELECT COUNT(*) FROM products WHERE carbon_kg_co2e_per_kg IS NOT NULL"),
            "with_allergen_data": one("SELECT COUNT(*) FROM products WHERE has_allergen_data"),
        }


def sanity_milk() -> list[tuple]:
    with db.connection() as conn:
        return list(db.q(conn, """
            SELECT p.product_id, p.sales_unit, p.name_en FROM products p
            WHERE p.category_code = '10400' AND EXISTS (SELECT 1 FROM product_claims c
              WHERE c.product_id = p.product_id AND c.claim = 'FREE_FROM_LACTOSE')
            ORDER BY p.product_id"""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--products", required=True)
    ap.add_argument("--reliability")
    ap.add_argument("--carbon")
    ap.add_argument("--inspect", action="store_true",
                    help="print the first raw record and its mapping, load nothing")
    args = ap.parse_args()

    if args.inspect:
        rec = next(iter_products(args.products))
        print(json.dumps(rec, indent=2, default=str, ensure_ascii=False)[:20000])
        print("---- mapped ----")
        print(json.dumps(transform(rec), indent=2, default=str, ensure_ascii=False))
        return 0

    with db.transaction() as conn:
        db.q(conn, "SET FOREIGN_KEY_CHECKS = 0")
        for t in CATALOG_TABLES:
            db.q(conn, f"TRUNCATE TABLE {t}")
        db.q(conn, "SET FOREIGN_KEY_CHECKS = 1")

    batch: list[dict] = []
    n = 0
    for rec in iter_products(args.products):
        batch.append(transform(rec))
        if len(batch) >= BATCH:
            flush(batch)
            n += len(batch)
            batch = []
            print(f"\r{n} products", end="", flush=True)
    flush(batch)
    n += len(batch)
    print(f"\r{n} products loaded")

    if args.reliability:
        update_column("delivery_reliability", read_csv(args.reliability, "deliveryReliability"))
    if args.carbon:
        update_column("carbon_kg_co2e_per_kg", read_csv(args.carbon, "AVG kg CO2-eq/kg"))

    ok = True
    print("\nRow counts (actual / expected):")
    for k, v in counts().items():
        mark = "ok" if v == EXPECTED[k] else "MISMATCH"
        ok &= v == EXPECTED[k]
        print(f"  {k:<20} {v:>7} / {EXPECTED[k]:>7}  {mark}")

    milk = sanity_milk()
    print(f"\nLactose-free products in category 10400: {len(milk)} (expected 35)")
    for pid, su, name in milk:
        print(f"  {pid}  {su:<4} {name}")
    return 0 if ok and len(milk) == 35 else 2


if __name__ == "__main__":
    raise SystemExit(main())
