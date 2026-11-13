"""Product search and detail over the static catalog (tasks T-101, T-102).

Document 03, section 3 (shapes) and document 06, section 4 (search semantics). Static data
only (FR-02): nothing here calls upstream, and nothing returns prices or availability (FR-05).

All SQL is parameterised. Lists of user values become numbered bind parameters.
"""

from __future__ import annotations

import base64
import binascii
import json
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Connection

from . import db
from .errors import invalid_request, not_found
from .models import (
    AllergenStatement,
    CodeName,
    Comparison,
    ProductDetail,
    ProductList,
    ProductSummary,
    UntrustedText,
)

MAX_LIMIT = 50
DEFAULT_LIMIT = 20
MIN_FULLTEXT_LEN = 3
UNTRUSTED_KEYS = (
    "marketing", "key_ingredients", "preparation_instructions", "usage_instructions",
    "storage_instructions",
)

_COLUMNS = (
    "p.product_id, p.sales_unit_gtin, p.name_en, p.name_fi, p.brand, p.vendor_name, "
    "p.category_code, p.sales_unit, p.country_of_origin, p.has_allergen_data, "
    "p.delivery_reliability, p.carbon_kg_co2e_per_kg, p.units_json"
)
_DETAIL_COLUMNS = (
    _COLUMNS + ", p.allowed_lot_size, p.variable_unit, p.temperature_condition, "
    "p.nutrition_json, p.ingredient_origins_json, p.untrusted_text_json"
)


# ---------------------------------------------------------------- helpers


def split_list(value: str | None) -> list[str]:
    """Comma list -> distinct, stripped, non-empty terms in order."""
    if not value:
        return []
    out: list[str] = []
    for part in value.split(","):
        p = part.strip()
        if p and p not in out:
            out.append(p)
    return out


def _like_escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _num(v: Any) -> int | float | None:
    """Upstream numbers arrive as strings like "20.0". Integral values become ints."""
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    return int(d) if d == d.to_integral_value() else float(d)


def _in_params(prefix: str, values: list[str]) -> tuple[str, dict[str, str]]:
    names = [f"{prefix}{i}" for i in range(len(values))]
    return ", ".join(f":{n}" for n in names), dict(zip(names, values, strict=True))


def _encode_cursor(doc: dict[str, Any]) -> str:
    raw = json.dumps(doc, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str, mode: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        doc = json.loads(raw)
        if not isinstance(doc, dict) or doc.get("m") != mode:
            raise ValueError
        return doc
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise invalid_request(
            "Cursor is not valid for this query.",
            [{"field": "cursor", "message": "Pass next_cursor unchanged with the same filters."}],
        ) from None


# ---------------------------------------------------------------- lookups and decoration


def _lookup(conn: Connection) -> dict[tuple[str, str], str]:
    rows = db.q(conn, "SELECT kind, code, name_en FROM code_lookup").all()
    return {(r[0], r[1]): r[2] for r in rows}


def _attributes(conn: Connection, ids: list[str]) -> dict[str, dict[str, Any]]:
    """Claims, labels, allergens and substitution flags for many products in four queries."""
    attrs: dict[str, dict[str, Any]] = {
        i: {"claims": [], "labels": [], "allergens": [], "subs": []} for i in ids
    }
    if not ids:
        return attrs
    ph, params = _in_params("id", ids)
    for pid, claim in db.q(conn, f"SELECT product_id, claim FROM product_claims "
                                 f"WHERE product_id IN ({ph}) ORDER BY claim", **params):
        attrs[pid]["claims"].append(claim)
    for pid, label in db.q(conn, f"SELECT product_id, label FROM product_labels "
                                 f"WHERE product_id IN ({ph}) ORDER BY label", **params):
        attrs[pid]["labels"].append(label)
    for pid, code, state in db.q(conn, f"SELECT product_id, allergen, state FROM "
                                       f"product_allergens WHERE product_id IN ({ph}) "
                                       f"ORDER BY allergen", **params):
        attrs[pid]["allergens"].append((code, state))
    for pid, sub in db.q(conn, f"SELECT product_id, substitute_product_id FROM "
                               f"product_substitutions WHERE product_id IN ({ph}) "
                               f"ORDER BY substitute_product_id", **params):
        attrs[pid]["subs"].append(sub)
    return attrs


def _case_size(units: list[dict[str, Any]]) -> float | None:
    for u in units:
        if u.get("unitId") == "KI":
            return _num(u.get("sizeInBaseUnits"))
    return None


def _summary_fields(
    row: dict[str, Any], attrs: dict[str, Any], names: dict[tuple[str, str], str]
) -> dict[str, Any]:
    units = db.jload(row["units_json"]) or []
    comparison: dict[str, Any] = {}
    if row["delivery_reliability"] is not None:
        comparison["delivery_reliability"] = float(row["delivery_reliability"])
    if row["carbon_kg_co2e_per_kg"] is not None:
        comparison["carbon_kg_co2e_per_kg"] = float(row["carbon_kg_co2e_per_kg"])
        comparison["carbon_basis"] = "product_group_average"
    return {
        "product_id": row["product_id"],
        "gtin": row["sales_unit_gtin"],
        "name": {"en": row["name_en"], "fi": row["name_fi"]},
        "brand": row["brand"],
        "vendor_name": row["vendor_name"],
        "category": CodeName(code=row["category_code"],
                             name=names.get(("category", row["category_code"]))),
        "sales_unit": CodeName(code=row["sales_unit"],
                               name=names.get(("unit", row["sales_unit"]))),
        "case_size_in_base_units": _case_size(units),
        "country_of_origin": row["country_of_origin"],
        "claims": attrs["claims"],
        "allergens": [AllergenStatement(code=c, name=names.get(("allergen", c)), state=s)
                      for c, s in attrs["allergens"]],
        "allergen_data": "present" if row["has_allergen_data"] else "missing",
        "labels": attrs["labels"],
        "comparison": Comparison(**comparison),
        "has_substitution_rules": bool(attrs["subs"]),
    }


# ---------------------------------------------------------------- search (T-101)


def search(
    conn: Connection,
    *,
    q: str | None = None,
    category: str | None = None,
    claims: list[str] | None = None,
    exclude_allergens: list[str] | None = None,
    labels: list[str] | None = None,
    name_excludes: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> ProductList:
    """Document 06, section 4. Without full-text `q`, order is delivery reliability DESC
    (nulls last), then product_id, with a keyset cursor. With full-text `q`, order is
    relevance and the cursor is an opaque offset."""
    limit = max(1, min(MAX_LIMIT, limit))
    where: list[str] = []
    params: dict[str, Any] = {}
    q = (q or "").strip() or None
    fulltext = bool(q and len(q) >= MIN_FULLTEXT_LEN)

    if q and fulltext:
        where.append("MATCH(p.name_en, p.name_fi) AGAINST (:q IN NATURAL LANGUAGE MODE)")
        params["q"] = q
    elif q:
        where.append("(p.name_en LIKE :ql OR p.name_fi LIKE :ql)")
        params["ql"] = f"%{_like_escape(q)}%"
    if category:
        where.append("p.category_code = :category")
        params["category"] = category
    for i, claim in enumerate(claims or []):
        where.append(f"EXISTS (SELECT 1 FROM product_claims c WHERE c.product_id = "
                     f"p.product_id AND c.claim = :claim{i})")
        params[f"claim{i}"] = claim
    for i, label in enumerate(labels or []):
        where.append(f"EXISTS (SELECT 1 FROM product_labels l WHERE l.product_id = "
                     f"p.product_id AND l.label = :label{i})")
        params[f"label{i}"] = label
    for i, code in enumerate(exclude_allergens or []):
        # Missing data is not safe: only an explicit FREE_FROM statement passes.
        where.append(f"EXISTS (SELECT 1 FROM product_allergens a WHERE a.product_id = "
                     f"p.product_id AND a.allergen = :alg{i} AND a.state = 'FREE_FROM')")
        params[f"alg{i}"] = code
    for i, term in enumerate(name_excludes or []):
        # Case- and accent-insensitive: "cafe" also excludes "Café" and "Cafè". The collation
        # is explicit so the result does not depend on the server's default.
        where.append(f"COALESCE(p.name_en, '') COLLATE utf8mb4_unicode_ci NOT LIKE :nx{i} "
                     f"AND COALESCE(p.name_fi, '') COLLATE utf8mb4_unicode_ci NOT LIKE :nx{i}")
        params[f"nx{i}"] = f"%{_like_escape(term)}%"

    if fulltext:
        offset = 0
        if cursor:
            offset = _decode_cursor(cursor, "o").get("o", 0)
            if not isinstance(offset, int) or offset < 0:
                raise invalid_request("Cursor is not valid for this query.")
        params["off"] = offset
        order = "ORDER BY MATCH(p.name_en, p.name_fi) AGAINST (:q IN NATURAL LANGUAGE MODE) " \
                "DESC, p.product_id LIMIT :lim OFFSET :off"
    else:
        if cursor:
            c = _decode_cursor(cursor, "k")
            pid, rel = c.get("p"), c.get("r")
            if not isinstance(pid, str):
                raise invalid_request("Cursor is not valid for this query.")
            params["cp"] = pid
            if rel is None:
                where.append("(p.delivery_reliability IS NULL AND p.product_id > :cp)")
            else:
                params["cr"] = str(rel)
                where.append("(p.delivery_reliability IS NULL OR p.delivery_reliability < :cr "
                             "OR (p.delivery_reliability = :cr AND p.product_id > :cp))")
        order = ("ORDER BY p.delivery_reliability IS NULL, p.delivery_reliability DESC, "
                 "p.product_id LIMIT :lim")

    params["lim"] = limit + 1
    sql = f"SELECT {_COLUMNS} FROM products p"
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = [dict(r) for r in db.q(conn, f"{sql} {order}", **params).mappings().all()]

    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        if fulltext:
            next_cursor = _encode_cursor({"m": "o", "o": params["off"] + limit})
        else:
            rel = last["delivery_reliability"]
            next_cursor = _encode_cursor(
                {"m": "k", "p": last["product_id"], "r": None if rel is None else str(rel)})

    names = _lookup(conn)
    attrs = _attributes(conn, [r["product_id"] for r in rows])
    items = [ProductSummary(**_summary_fields(r, attrs[r["product_id"]], names)) for r in rows]
    return ProductList(items=items, next_cursor=next_cursor, count=len(items))


# ---------------------------------------------------------------- detail (T-102)


def _nutrition(raw: Any) -> list[dict[str, Any]]:
    out = []
    for n in raw or []:
        out.append({
            "id": n.get("id"),
            "value": _num(n.get("value")),
            "unit": n.get("unit"),
            "per": {"value": _num(n.get("basisQuantity")), "unit": n.get("basisQuantityUnit")},
        })
    return out


def _ingredient_origins(raw: Any) -> list[dict[str, Any]]:
    """One entry per (ingredient, origin). Ingredients without an origin are left out."""
    out = []
    for ing in raw or []:
        names = ing.get("names") or []
        name = next((n.get("value") for n in names if n.get("language") == "en"), None)
        if name is None and names:
            name = names[0].get("value")
        for o in ing.get("origins") or []:
            out.append({
                "ingredient": name.lower() if name else None,
                "type": o.get("type"),
                "country": o.get("country"),
            })
    return out


def _untrusted(raw: Any) -> UntrustedText:
    raw = raw or {}
    fields = {k: raw[k] for k in UNTRUSTED_KEYS if isinstance(raw.get(k), dict) and raw[k]}
    return UntrustedText(**fields)


def detail(conn: Connection, product_id: str) -> ProductDetail:
    row = db.q(conn, f"SELECT {_DETAIL_COLUMNS} FROM products p WHERE p.product_id = :pid",
               pid=product_id).mappings().first()
    if row is None:
        raise not_found(f"Product {product_id} does not exist.")
    row = dict(row)
    names = _lookup(conn)
    attrs = _attributes(conn, [product_id])[product_id]
    units = [
        {"unit": u.get("unitId"), "size_in_base_units": _num(u.get("sizeInBaseUnits")),
         "gtin": u.get("gtin")}
        for u in db.jload(row["units_json"]) or []
    ]
    return ProductDetail(
        **_summary_fields(row, attrs, names),
        units=units,
        allowed_lot_size=float(row["allowed_lot_size"]),
        variable_unit=bool(row["variable_unit"]),
        temperature_condition=row["temperature_condition"],
        nutrition=_nutrition(db.jload(row["nutrition_json"])),
        ingredient_origins=_ingredient_origins(db.jload(row["ingredient_origins_json"])),
        substitutes=attrs["subs"],
        untrusted_text=_untrusted(db.jload(row["untrusted_text_json"])),
    )
