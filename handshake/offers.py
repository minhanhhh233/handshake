"""Offer builder (tasks T-105, T-106, T-108; document 06, sections 5 and 6).

Pure parts (no I/O), reusable and unit-tested without a database:

- `convert()`               quantity conversion method and litres/kilograms per sales unit
- `sales_units_needed()`    sales units to order for a base quantity (lot size rounding)
- `mandate_product_reasons()` mandate rules R2, R3, R4 for one product (defined in `policy`)

Database parts:

- `build_offer()`           the whole POST /v1/offers flow
- `load_offer()`, `expire_if_due()`, `release_holds()`  used by GET /v1/offers and by orders
- `held_quantities()`       held and committed sales units per product (document 04, section 4)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import Connection, bindparam, text

from . import db, events, mandates, signing, timeutil, upstream
from .config import get_settings
from .errors import HandshakeError, forbidden, invalid_request, no_candidates
from .ids import new_id
from .models import (
    AllergenStatement,
    ClaimSource,
    ClaimsSource,
    Comparison,
    ComparisonPrice,
    Conversion,
    Differences,
    Excluded,
    FulfilmentSummary,
    LineAttributes,
    LineAvailability,
    Mandate,
    Offer,
    OfferLine,
    OfferRequest,
    OfferRequestLine,
    PriceValid,
    ProductFacts,
    Quantity,
    Totals,
    UpstreamFacts,
    money,
)
# Pure mandate rules R2/R3/R4 live in the policy engine; re-exported here (T-201).
from .policy import ALLERGEN_NAMES, allergen_label, mandate_product_reasons

log = logging.getLogger("handshake.offers")

BASE_UNITS = ("L", "KG")
QUANTITY_UNITS = ("L", "KG", "SALES_UNIT")
UNKNOWN_AMOUNT = Decimal("999999")  # document 06, section 1.4; Q-15
CONVERSION_TOLERANCE = Decimal("0.02")
MAX_CANDIDATES_PER_LINE = 25  # NFR-02; document 06, section 6 step 1
MAX_SAME_CATEGORY = 10  # Q-14
OFFICIAL_SOURCE = "valio_aimo_substitution_rule"
SAME_CATEGORY_SOURCE = "same_category_same_claims"
_CENT = Decimal("0.01")
_BASIS = Decimal("0.0001")
_QTY = Decimal("0.001")

# ================================================================ pure: quantity conversion


def convert(
    requested_unit: str,
    unit_price: Decimal | None,
    comparison_price: Decimal | None,
    comparison_unit: str | None,
    net_volume_l: Decimal | None,
    net_weight_kg: Decimal | None,
    *,
    product_id: str = "",
) -> Conversion | None:
    """Litres or kilograms per sales unit (document 06, section 5). None = no conversion.

    1. price_ratio: comparison unit equals the requested unit and both prices are positive.
    2. product_data: the loader's net volume (L) or net weight (KG) per sales unit.
    If both exist and differ by more than 2 %, price_ratio wins and a warning is logged.
    """
    unit = requested_unit.upper()
    if unit not in BASE_UNITS:
        return None
    ratio: Decimal | None = None
    if (
        comparison_unit is not None and comparison_unit.strip().upper() == unit
        and unit_price is not None and comparison_price is not None
        and unit_price > 0 and comparison_price > 0
    ):
        ratio = (Decimal(unit_price) / Decimal(comparison_price)).quantize(
            _BASIS, rounding=ROUND_HALF_UP)
        if ratio <= 0:
            ratio = None
    data = net_volume_l if unit == "L" else net_weight_kg
    data = Decimal(data) if data is not None and Decimal(data) > 0 else None
    if ratio is not None:
        if data is not None and abs(ratio - data) / data > CONVERSION_TOLERANCE:
            log.warning(
                "conversion methods disagree for %s: price_ratio %s, product_data %s %s; "
                "using price_ratio", product_id or "?", ratio, data, unit,
            )
        return Conversion(base_per_sales_unit=ratio, method="price_ratio")
    if data is not None:
        return Conversion(base_per_sales_unit=data, method="product_data")
    return None


def _lot_step(allowed_lot_size: Decimal | None, variable_unit: bool) -> Decimal:
    """Ordering step in sales units: the lot size if >= 1, else 1 unless variable (A-04)."""
    lot = Decimal(allowed_lot_size) if allowed_lot_size is not None else Decimal(0)
    if lot >= 1:
        return lot
    if variable_unit and lot > 0:
        return lot
    return Decimal(1)


def sales_units_needed(
    base_quantity: Decimal, base_per_sales_unit: Decimal,
    allowed_lot_size: Decimal | None = Decimal("0.01"), variable_unit: bool = False,
) -> Decimal:
    """units = ceil(base / per_unit), rounded up to the ordering step (document 06, section 5)."""
    if base_quantity <= 0:
        return Decimal(0)
    step = _lot_step(allowed_lot_size, variable_unit)
    raw = Decimal(base_quantity) / Decimal(base_per_sales_unit)
    if step == 1:
        units = raw.to_integral_value(rounding=ROUND_CEILING)
    else:
        units = (raw / step).to_integral_value(rounding=ROUND_CEILING) * step
        if step < 1:
            units = units.quantize(_QTY)
    return units


def round_down_to_step(
    units: Decimal, allowed_lot_size: Decimal | None = Decimal("0.01"),
    variable_unit: bool = False,
) -> Decimal:
    """Largest orderable quantity not above `units` (used for capacity limits)."""
    if units <= 0:
        return Decimal(0)
    step = _lot_step(allowed_lot_size, variable_unit)
    return (Decimal(units) / step).to_integral_value(rounding=ROUND_FLOOR) * step


# ================================================================ catalog


@dataclass
class CatalogProduct:
    product_id: str
    name: str | None
    category_code: str
    sales_unit: str
    allowed_lot_size: Decimal
    variable_unit: bool
    has_allergen_data: bool
    net_volume_l: Decimal | None
    net_weight_kg: Decimal | None
    delivery_reliability: float | None
    carbon_kg_co2e_per_kg: float | None
    country_of_origin: str | None
    claims: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    allergens: dict[str, str] = field(default_factory=dict)

    def facts(self) -> ProductFacts:
        return ProductFacts(
            product_id=self.product_id, category_code=self.category_code,
            claims=self.claims, labels=self.labels, allergens=self.allergens,  # type: ignore[arg-type]
            has_allergen_data=self.has_allergen_data,
        )


def _in(sql: str, _ids: list[str] | None = None):  # noqa: ANN202
    return text(sql).bindparams(bindparam("ids", expanding=True))


def load_catalog(conn: Connection, product_ids: list[str]) -> dict[str, CatalogProduct]:
    """Static data for the given products (products and the three attribute tables)."""
    ids = sorted(set(product_ids))
    if not ids:
        return {}
    rows = conn.execute(_in(
        "SELECT product_id, name_en, name_fi, category_code, sales_unit, allowed_lot_size, "
        "variable_unit, has_allergen_data, net_volume_l_per_sales_unit, "
        "net_weight_kg_per_sales_unit, delivery_reliability, carbon_kg_co2e_per_kg, "
        "country_of_origin FROM products WHERE product_id IN :ids", ids), {"ids": ids},
    ).mappings().all()
    out: dict[str, CatalogProduct] = {}
    for r in rows:
        out[r["product_id"]] = CatalogProduct(
            product_id=r["product_id"], name=r["name_en"] or r["name_fi"],
            category_code=r["category_code"], sales_unit=r["sales_unit"],
            allowed_lot_size=Decimal(r["allowed_lot_size"]),
            variable_unit=bool(r["variable_unit"]),
            has_allergen_data=bool(r["has_allergen_data"]),
            net_volume_l=r["net_volume_l_per_sales_unit"],
            net_weight_kg=r["net_weight_kg_per_sales_unit"],
            delivery_reliability=(float(r["delivery_reliability"])
                                  if r["delivery_reliability"] is not None else None),
            carbon_kg_co2e_per_kg=(float(r["carbon_kg_co2e_per_kg"])
                                   if r["carbon_kg_co2e_per_kg"] is not None else None),
            country_of_origin=r["country_of_origin"],
        )
    if not out:
        return out
    found = sorted(out)
    # One round trip for the three attribute tables (UNION ALL) instead of three.
    stmt = text(
        "SELECT 'c' AS k, product_id, claim AS v, NULL AS s FROM product_claims "
        "WHERE product_id IN :ids "
        "UNION ALL SELECT 'l', product_id, label, NULL FROM product_labels "
        "WHERE product_id IN :ids "
        "UNION ALL SELECT 'a', product_id, allergen, CAST(state AS CHAR) FROM product_allergens "
        "WHERE product_id IN :ids "
        "ORDER BY k, product_id, v").bindparams(bindparam("ids", expanding=True))
    for kind, pid, value, state in conn.execute(stmt, {"ids": found}):
        if kind == "c":
            out[pid].claims.append(value)
        elif kind == "l":
            out[pid].labels.append(value)
        else:
            out[pid].allergens[value] = state
    return out


def substitute_ids(
    conn: Connection, line: OfferRequestLine, requested: CatalogProduct, listed: list[str],
) -> list[tuple[str, str]]:
    """Gateway-added substitutes for a line with `substitution: allowed` (document 06, 6 step 1).

    (a) Official substitutes of the requested product, by product ID; then (b) up to 10
    products in the requested product's category that have every required claim and are
    declared free from every forbidden allergen of the line's requirements, by delivery
    reliability (highest first, unknown last) then product ID (Q-14). Products already listed
    are skipped; the line never exceeds 25 candidates. Returns (product_id, source) pairs.
    """
    room = MAX_CANDIDATES_PER_LINE - len(listed)
    if room <= 0:
        return []
    seen = set(listed)
    out: list[tuple[str, str]] = []
    for (pid,) in conn.execute(text(
            "SELECT s.substitute_product_id FROM product_substitutions s "
            "JOIN products p ON p.product_id = s.substitute_product_id "
            "WHERE s.product_id = :p ORDER BY s.substitute_product_id"),
            {"p": requested.product_id}):
        if pid not in seen and len(out) < room:
            seen.add(pid)
            out.append((pid, OFFICIAL_SOURCE))
    limit = min(MAX_SAME_CATEGORY, room - len(out))
    if limit <= 0:
        return out
    claims = sorted(set(line.requirements.required_claims))
    allergens = sorted(set(line.requirements.forbidden_allergens))
    sql = ("SELECT p.product_id FROM products p WHERE p.category_code = :c "
           "AND p.product_id NOT IN :seen")
    params: dict[str, Any] = {"c": requested.category_code, "seen": sorted(seen)}
    binds = [bindparam("seen", expanding=True)]
    if claims:
        sql += (" AND (SELECT COUNT(DISTINCT pc.claim) FROM product_claims pc "
                "WHERE pc.product_id = p.product_id AND pc.claim IN :claims) = :nc")
        params |= {"claims": claims, "nc": len(claims)}
        binds.append(bindparam("claims", expanding=True))
    if allergens:
        sql += (" AND (SELECT COUNT(DISTINCT pa.allergen) FROM product_allergens pa "
                "WHERE pa.product_id = p.product_id AND pa.allergen IN :alg "
                "AND pa.state = 'FREE_FROM') = :na")
        params |= {"alg": allergens, "na": len(allergens)}
        binds.append(bindparam("alg", expanding=True))
    sql += (" ORDER BY p.delivery_reliability IS NULL, p.delivery_reliability DESC, "
            "p.product_id LIMIT :lim")
    params["lim"] = limit
    for (pid,) in conn.execute(text(sql).bindparams(*binds), params):
        out.append((pid, SAME_CATEGORY_SOURCE))
    return out


# ================================================================ holds


def agent_sandbox(conn: Connection, agent_id: str) -> str | None:
    """The public-demo sandbox an agent belongs to, or None for a real customer's agent."""
    return db.q(conn, "SELECT c.sandbox_id FROM agents a JOIN customers c "
                      "ON c.customer_id = a.customer_id WHERE a.agent_id = :a",
                a=agent_id).scalar()


def held_quantities(
    conn: Connection, product_ids: list[str], delivery_date: date, *, lock: bool = False,
    now: datetime | None = None, sandbox_id: str | None = None,
) -> dict[str, Decimal]:
    """Sales units committed or held (unexpired) per product for the date (document 04, 4).

    An expired `held` row counts as released even before it is updated (A-12). Holds are
    counted within one scope: a public-demo sandbox sees only its own holds, and real
    customers see only holds of agents outside any sandbox (document 09, section 6c).
    """
    ids = sorted(set(product_ids))
    if not ids:
        return {}
    now = now or timeutil.now()
    sql = ("SELECT h.product_id, h.quantity_sales_units FROM stock_holds h "
           "LEFT JOIN offers o ON o.offer_id = h.offer_id "
           "LEFT JOIN agents a ON a.agent_id = o.agent_id "
           "LEFT JOIN customers c ON c.customer_id = a.customer_id "
           "WHERE h.product_id IN :ids AND h.delivery_date = :d AND (h.status = 'committed' "
           "OR (h.status = 'held' AND h.expires_at > :now)) AND c.sandbox_id <=> :sbx")
    if lock:
        sql += " FOR UPDATE"
    out: dict[str, Decimal] = {}
    for pid, qty in conn.execute(_in(sql, ids),
                                 {"ids": ids, "d": delivery_date, "now": timeutil.to_db(now),
                                  "sbx": sandbox_id}):
        out[pid] = out.get(pid, Decimal(0)) + Decimal(qty)
    return out


# ================================================================ validation


def validate_request(req: OfferRequest, catalog_ids: set[str] | None = None) -> None:
    """Rules the Pydantic model cannot express (document 03, section 5). Raises 400."""
    errors: list[dict[str, Any]] = []
    if req.delivery_date < timeutil.today_helsinki():
        errors.append({"field": "delivery_date",
                       "message": "delivery_date must be today or later (Europe/Helsinki)."})
    for i, line in enumerate(req.lines):
        unit = line.quantity.unit
        if unit not in QUANTITY_UNITS:
            errors.append({"field": f"lines.{i}.quantity.unit",
                           "message": "unit must be L, KG or SALES_UNIT."})
        if line.quantity.value <= 0:
            errors.append({"field": f"lines.{i}.quantity.value",
                           "message": "quantity must be greater than 0."})
        if unit == "SALES_UNIT" and (len(line.product_ids) != 1 or line.fulfilment != "exact"):
            errors.append({"field": f"lines.{i}.quantity.unit",
                           "message": "SALES_UNIT needs exactly one product ID and "
                                      "fulfilment 'exact'."})
    if catalog_ids is not None:
        unknown = sorted({p for line in req.lines for p in line.product_ids} - catalog_ids)
        if unknown:
            errors.append({"field": "lines.product_ids",
                           "message": "Unknown product IDs: " + ", ".join(unknown)})
    if errors:
        detail = "; ".join(e["message"] for e in errors)
        raise invalid_request(detail, errors)


# ================================================================ building blocks


def price_str(value: Decimal) -> str:
    """Money string: two places, or up to four when the upstream value needs them."""
    v = Decimal(value)
    if v == v.quantize(_CENT):
        return money(v)
    s = str(v.quantize(_BASIS, rounding=ROUND_HALF_UP))
    whole, _, frac = s.partition(".")
    frac = frac.rstrip("0")
    return f"{whole}.{frac.ljust(2, '0')}"


def _num(d: Decimal) -> Decimal:
    """Quantity for output: three places at most, trailing zeros dropped."""
    q = Decimal(d).quantize(_QTY, rounding=ROUND_HALF_UP)
    return q.to_integral_value() if q == q.to_integral_value() else q.normalize()


@dataclass
class Candidate:
    request_line: int  # 1-based
    product: CatalogProduct
    role: str  # requested | alternate | substitute
    pos: int = 0  # place in the line's order of use (0 = requested)
    source: str | None = None  # substitution source, substitutes only
    substitute_for: str | None = None  # the line's requested product, substitutes only


@dataclass
class Prepared:
    """A candidate that passed every filter, with what the offer line needs."""

    cand: Candidate
    facts: UpstreamFacts
    unit_excl: Decimal
    unit_incl: Decimal
    tax_rate: Decimal
    conversion: Conversion | None  # None for SALES_UNIT requests
    per_unit: Decimal  # base units per sales unit; 1 for SALES_UNIT
    capacity: Decimal | None  # upstream amount for Stock with a known amount; None = unlimited
    assurance: str


def _excluded(cand: Candidate, reason: str, message: str, **kw: Any) -> Excluded:
    return Excluded(product_id=cand.product.product_id, request_line=cand.request_line,
                    reason=reason, message=message, **kw)  # type: ignore[arg-type]


def static_filter(
    line: OfferRequestLine, cand: Candidate, mandate: Mandate
) -> Excluded | None:
    """Requirements, then mandate rules R2/R3/R4 with effect rejected (document 06, 6 step 2)."""
    p = cand.product
    missing = [c for c in line.requirements.required_claims if c not in p.claims]
    not_free = [a for a in line.requirements.forbidden_allergens
                if p.allergens.get(a) != "FREE_FROM"]
    if missing or not_free:
        parts = []
        if missing:
            parts.append("lacks the required claim(s) " + ", ".join(missing))
        if not_free:
            parts.append("is not declared free from " + ", ".join(
                allergen_label(a) for a in not_free))
        return _excluded(cand, "fails_requirements",
                         f"Product {p.product_id} " + " and ".join(parts) + ".")
    rejected = [r for r in mandate_product_reasons(p.facts(), mandate) if r.effect == "rejected"]
    if rejected:
        return _excluded(cand, "forbidden_by_mandate", rejected[0].message,
                         policy_reason=rejected[0])
    return None


def dynamic_filter(
    line: OfferRequestLine, cand: Candidate, f: UpstreamFacts, delivery_date: date,
    now: datetime,
) -> Prepared | Excluded:
    """Price, availability, cut-off and conversion checks (document 06, 6 step 4)."""
    pid = cand.product.product_id
    price, av = f.price, f.availability
    if (price is None or price.error or price.unit_price_excl_tax is None
            or price.unit_price_excl_tax <= 0):
        return _excluded(cand, "price_unavailable", f"Upstream has no price for product {pid}.")
    unit_excl = Decimal(price.unit_price_excl_tax)
    tax_rate = price.tax_rate_percent
    unit_incl = price.unit_price_incl_tax
    if unit_incl is None and tax_rate is not None:
        unit_incl = (unit_excl * (1 + Decimal(tax_rate) / 100)).quantize(_BASIS, ROUND_HALF_UP)
    if tax_rate is None and unit_incl is not None:
        tax_rate = ((Decimal(unit_incl) / unit_excl - 1) * 100).quantize(_CENT, ROUND_HALF_UP)
    if unit_incl is None or tax_rate is None:
        return _excluded(cand, "price_unavailable",
                         f"Upstream gives no tax rate for product {pid}.")
    if av is None or not av.has_schedule or av.available_date is None:
        return _excluded(cand, "out_of_stock",
                         f"Product {pid} is not deliverable on any date (no schedule line).")
    if av.sales_block:
        return _excluded(cand, "sales_blocked", f"Product {pid} is sales-blocked upstream.")
    if av.available_amount is not None and av.available_amount <= 0:
        return _excluded(cand, "out_of_stock", f"Product {pid} has no stock available.")
    if av.closing_time is not None and av.closing_time <= now:
        return _excluded(
            cand, "not_deliverable_by_date",
            f"The order cut-off for product {pid} ({timeutil.iso(av.closing_time)}) has passed, "
            f"and upstream gives no later delivery date.",
            available_date=None, order_by=av.closing_time,
        )
    if av.available_date > delivery_date:
        by = f" Order by {timeutil.iso(av.closing_time)} for that date." if av.closing_time else ""
        return _excluded(
            cand, "not_deliverable_by_date",
            f"Product {pid} is available from {av.available_date.isoformat()}, after the "
            f"requested {delivery_date.isoformat()}.{by}",
            available_date=av.available_date, order_by=av.closing_time,
        )
    unit = line.quantity.unit
    if unit == "SALES_UNIT":
        conversion, per_unit = None, Decimal(1)
    else:
        conversion = convert(unit, unit_excl, price.comparison_price, price.comparison_unit,
                             cand.product.net_volume_l, cand.product.net_weight_kg,
                             product_id=pid)
        if conversion is None:
            return _excluded(cand, "no_quantity_conversion",
                             f"Cannot tell how many {unit} one sales unit of product {pid} "
                             f"contains.")
        per_unit = Decimal(conversion.base_per_sales_unit)
    known = av.available_amount is not None and av.available_amount < UNKNOWN_AMOUNT
    capacity = Decimal(av.available_amount) if (av.logistic_model == "stock" and known) else None
    assurance = "confirmed" if av.logistic_model == "stock" else "unconfirmed"
    return Prepared(cand=cand, facts=f, unit_excl=unit_excl, unit_incl=Decimal(unit_incl),
                    tax_rate=Decimal(tax_rate), conversion=conversion, per_unit=per_unit,
                    capacity=capacity, assurance=assurance)


@dataclass
class FilledLine:
    prep: Prepared
    units: Decimal
    base: Decimal | None


def fill_line(
    line: OfferRequestLine, prepared: list[Prepared], used: dict[str, Decimal],
    held: dict[str, Decimal],
) -> tuple[list[FilledLine], list[Excluded]]:
    """Fill one request line in preference order (document 06, 6 steps 5 to 7).

    `used` tracks sales units already taken by earlier lines of the same request and is
    updated. `held` is the committed and unexpired held quantity per product.
    """
    remaining = Decimal(line.quantity.value)
    out: list[FilledLine] = []
    excluded: list[Excluded] = []
    for prep in prepared:
        if remaining <= 0:
            break
        p = prep.cand.product
        pid = p.product_id
        need = sales_units_needed(remaining, prep.per_unit, p.allowed_lot_size, p.variable_unit)
        if prep.capacity is not None:
            cap = prep.capacity - held.get(pid, Decimal(0)) - used.get(pid, Decimal(0))
            cap = round_down_to_step(cap, p.allowed_lot_size, p.variable_unit)
            if cap <= 0:
                excluded.append(_excluded(
                    prep.cand, "out_of_stock",
                    f"No unreserved stock of product {pid} is left for the delivery date."))
                continue
            take = min(need, cap)
        else:
            take = need
        if take <= 0:
            continue
        used[pid] = used.get(pid, Decimal(0)) + take
        base = take * prep.per_unit
        remaining -= base
        out.append(FilledLine(prep, take,
                              base if line.quantity.unit in BASE_UNITS else None))
        if line.fulfilment == "exact":
            break
    return out, excluded


def _comparison(f: UpstreamFacts | None) -> tuple[Decimal, str] | None:
    """Upstream comparison price per L/KG and its unit, or None when not priced."""
    price = f.price if f is not None else None
    if (price is None or price.error or price.unit_price_excl_tax is None
            or price.unit_price_excl_tax <= 0 or price.comparison_price is None
            or price.comparison_price <= 0 or not price.comparison_unit):
        return None
    return Decimal(price.comparison_price), price.comparison_unit.strip().upper()


def differences(
    p: CatalogProduct, requested: CatalogProduct, this_facts: UpstreamFacts | None,
    requested_facts: UpstreamFacts | None, source: str | None,
) -> Differences:
    """How an alternate or substitute differs from the line's requested product (doc 03, 5).

    Price: comparison price per base unit of this product against the requested product's,
    both from upstream; null unless both are priced in the same unit. Claims, labels, origin
    and delivery reliability come from the catalog.
    """
    pct: float | None = None
    a, b = _comparison(this_facts), _comparison(requested_facts)
    if a is not None and b is not None and a[1] == b[1]:
        pct = float(((a[0] - b[0]) / b[0] * 100).quantize(Decimal("0.1"), ROUND_HALF_UP))
    delta: float | None = None
    if p.delivery_reliability is not None and requested.delivery_reliability is not None:
        delta = float((Decimal(str(p.delivery_reliability))
                       - Decimal(str(requested.delivery_reliability))).quantize(
            _QTY, ROUND_HALF_UP))
    origin = (p.country_of_origin or "").strip().upper()
    req_origin = (requested.country_of_origin or "").strip().upper()
    return Differences(
        price_per_base_unit_percent=pct,
        claims_lost=sorted(set(requested.claims) - set(p.claims)),
        claims_gained=sorted(set(p.claims) - set(requested.claims)),
        labels_lost=sorted(set(requested.labels) - set(p.labels)),
        labels_gained=sorted(set(p.labels) - set(requested.labels)),
        # Known only when both origins are known; null otherwise (decided 2026-11-14).
        origin_changed=(origin != req_origin) if origin and req_origin else None,
        delivery_reliability_delta=delta,
        source=source,  # type: ignore[arg-type]
    )


@dataclass
class LineRef:
    """The requested product of a request line, for `differences`."""

    product: CatalogProduct
    facts: UpstreamFacts | None


def _offer_line(line_no: int, fl: FilledLine, request_unit: str, ref: LineRef | None = None
                ) -> OfferLine:
    prep, p, f = fl.prep, fl.prep.cand.product, fl.prep.facts
    diff = None
    if prep.cand.role != "requested" and ref is not None:
        diff = differences(p, ref.product, f, ref.facts, prep.cand.source)
    price, av = f.price, f.availability
    assert price is not None and av is not None
    comparison_price = None
    if price.comparison_price is not None and price.comparison_price > 0 and price.comparison_unit:
        comparison_price = ComparisonPrice(value=price_str(price.comparison_price),
                                           unit=price.comparison_unit.upper())
    carbon = p.carbon_kg_co2e_per_kg
    return OfferLine(
        line_no=line_no, request_line=prep.cand.request_line, product_id=p.product_id,
        name=p.name, role=prep.cand.role,  # type: ignore[arg-type]
        substitute_for=prep.cand.substitute_for,
        quantity=Quantity(value=_num(fl.units), unit="SALES_UNIT"),
        sales_unit=p.sales_unit or (av.sales_unit or ""),
        base_quantity=(Quantity(value=_num(fl.base), unit=request_unit)
                       if fl.base is not None else None),
        conversion=prep.conversion,
        unit_price_excl_tax=price_str(prep.unit_excl),
        comparison_price=comparison_price,
        tax_rate_percent=money(prep.tax_rate),
        line_total_excl_tax=money(fl.units * prep.unit_excl),
        # Invoice rounding (A-25): excl. total x (1 + rate), rounded once per line.
        # Upstream valueWithTax is kept in the price snapshot only.
        line_total_incl_tax=money(
            Decimal(money(fl.units * prep.unit_excl)) * (1 + prep.tax_rate / 100)),
        price_valid=PriceValid(from_=price.valid_from, to=price.valid_to),
        availability=LineAvailability(
            available_date=av.available_date, order_by=av.closing_time,
            logistic_model=av.logistic_model, assurance=prep.assurance,  # type: ignore[arg-type]
        ),
        attributes=LineAttributes(
            claims=list(p.claims),
            allergens=[AllergenStatement(code=c, state=s)  # type: ignore[arg-type]
                       for c, s in sorted(p.allergens.items())],
            allergen_data="present" if p.has_allergen_data else "missing",
            labels=list(p.labels), country_of_origin=p.country_of_origin,
        ),
        comparison=Comparison(
            delivery_reliability=p.delivery_reliability,
            carbon_kg_co2e_per_kg=carbon,
            carbon_basis="product_group_average" if carbon is not None else None,
        ),
        differences=diff,
    )


def _qty_text(q: Quantity) -> str:
    v = q.value
    s = str(int(v)) if v == v.to_integral_value() else str(v.normalize())
    return f"{s} {'units' if q.unit == 'SALES_UNIT' else q.unit}"


# ================================================================ main flow


def build_offer(req: OfferRequest, *, agent_id: str, customer_id: str, trace_id: str,
                ttl_seconds: int | None = None) -> dict[str, Any]:
    """POST /v1/offers (document 03, section 5; document 06, section 6). Returns the 201 body.

    Raises HandshakeError: invalid_request, forbidden (no mandate), no_candidates,
    upstream_unavailable. No offer row exists unless the call returns.
    """
    settings = get_settings()
    all_ids = [p for line in req.lines for p in line.product_ids]
    # Step 1, candidates: the agent's IDs, then (substitution 'allowed') official substitutes
    # and same-category products, at most 25 per line (document 06, section 6). Looked up on
    # the same connection as the mandate and catalog (one checkout, one pre-ping), and only
    # when the request is valid, so error behaviour is unchanged.
    subs: dict[int, list[tuple[str, str]]] = {}
    with db.connection() as conn:
        mandate = mandates.get_current(conn, agent_id)
        catalog = load_catalog(conn, all_ids)
        if (mandate is not None and set(all_ids) <= set(catalog)
                and any(line.substitution == "allowed" for line in req.lines)):
            for i, line in enumerate(req.lines, start=1):
                if line.substitution == "allowed":
                    listed = list(dict.fromkeys(line.product_ids))
                    subs[i] = substitute_ids(conn, line, catalog[listed[0]], listed)
            extra = load_catalog(conn, [pid for s in subs.values() for pid, _ in s])
            catalog = {**extra, **catalog}
    if mandate is None:
        raise forbidden("This agent has no mandate. An admin must set one before offers "
                        "can be issued.")
    validate_request(req, set(catalog))
    if req.replaces_offer_id:
        withdraw_offer(req.replaces_offer_id, agent_id)

    # offer.requested in its own transaction, before upstream, so failures stay traceable.
    total_candidates = len({p for line in req.lines for p in line.product_ids})
    req_text = "; ".join(
        f"{_qty_text(line.quantity)} from {len(dict.fromkeys(line.product_ids))} "
        f"candidate product(s)" for line in req.lines)
    with db.transaction() as conn:
        events.append(
            conn, trace_id=trace_id, type="offer.requested", actor_type="agent",
            actor_id=agent_id, audience="both",
            text=f"Agent requested an offer for {req_text}, delivery "
                 f"{req.delivery_date.isoformat()}.",
            body={"delivery_date": req.delivery_date.isoformat(), "lines": [
                {"product_ids": line.product_ids,
                 "quantity": line.quantity.dump(), "fulfilment": line.fulfilment,
                 "substitution": line.substitution, "prefer": line.prefer,
                 "requirements": line.requirements.dump()} for line in req.lines],
                "max_options": req.max_options, "candidates": total_candidates,
                **({"replaces_offer_id": req.replaces_offer_id}
                   if req.replaces_offer_id else {})},
        )

    # Step 2, static filter.
    excluded: list[Excluded] = []
    survivors: list[list[Candidate]] = []
    order: dict[tuple[int, str], int] = {}  # (request line, product) -> place, for excluded[]
    for i, line in enumerate(req.lines, start=1):
        listed = list(dict.fromkeys(line.product_ids))
        cands = [Candidate(i, catalog[pid], "requested" if j == 0 else "alternate", pos=j)
                 for j, pid in enumerate(listed)]
        cands += [Candidate(i, catalog[pid], "substitute", pos=len(listed) + k, source=src,
                            substitute_for=listed[0])
                  for k, (pid, src) in enumerate(subs.get(i, [])) if pid in catalog]
        kept: list[Candidate] = []
        for cand in cands:
            order[(i, cand.product.product_id)] = cand.pos
            ex = static_filter(line, cand, mandate)
            if ex:
                excluded.append(ex)
            else:
                kept.append(cand)
        survivors.append(kept)

    def by_line() -> list[dict[str, Any]]:
        """excluded[] grouped by request line, each in the line's candidate order."""
        excluded.sort(key=lambda e: (e.request_line or 0,
                                     order.get((e.request_line or 0, e.product_id), 0)))
        return [e.dump() for e in excluded]

    query_ids = list(dict.fromkeys(c.product.product_id for kept in survivors for c in kept))
    if not query_ids:
        raise no_candidates(by_line())

    facts = upstream.get_adapter().get_facts(
        query_ids, req.delivery_date, trace_id=trace_id, actor_id=agent_id)

    now = timeutil.now()
    prepared: list[list[Prepared]] = []
    for line, kept in zip(req.lines, survivors, strict=True):
        ok: list[Prepared] = []
        for cand in kept:
            f = facts.get(cand.product.product_id) or UpstreamFacts(
                product_id=cand.product.product_id, price=None, availability=None)
            r = dynamic_filter(line, cand, f, req.delivery_date, now)
            if isinstance(r, Excluded):
                excluded.append(r)
            else:
                ok.append(r)
        if line.prefer == "lowest_price":
            ok.sort(key=lowest_price_key)
        prepared.append(ok)

    ranked = req.max_options > 1
    scores = [line_scores(ok, mandate) for ok in prepared] if ranked else []
    variants = ranked_variants(prepared, scores) if ranked else [("best", prepared)]

    with db.transaction() as conn:
        limited = [p.cand.product.product_id for ok in prepared for p in ok
                   if p.capacity is not None]
        held = held_quantities(conn, limited, req.delivery_date, lock=True, now=now,
                               sandbox_id=agent_sandbox(conn, agent_id))
        refs = []
        for line in req.lines:
            rid = line.product_ids[0]
            refs.append(LineRef(catalog[rid], facts.get(rid)))
        options: list[dict[str, Any]] = []
        seen: list[tuple[Any, ...]] = []
        for n, (label, plist) in enumerate(variants):
            used: dict[str, Decimal] = {}  # each option holds its own stock (A-22)
            filled: list[list[FilledLine]] = []
            for line, ok in zip(req.lines, plist, strict=True):
                fls, ex = fill_line(line, ok, used, held)
                if n == 0:
                    excluded.extend(ex)
                filled.append(fls)
            shape = tuple(tuple((fl.prep.cand.product.product_id, fl.units) for fl in fls)
                          for fls in filled)
            if not any(filled) or shape in seen:
                continue  # an option is returned only if it differs from the others
            seen.append(shape)
            offer = _assemble(req, filled, mandate, agent_id, customer_id, now, settings, refs,
                              ttl_seconds=ttl_seconds)
            payload = offer.dump()
            jws = signing.sign(payload, signing.TYP_OFFER)
            _persist(conn, offer, payload, jws, req, filled, trace_id)
            opt: dict[str, Any] = {"label": label, "offer": payload, "jws": jws}
            if ranked:
                opt["score"] = option_score(filled, scores)
            options.append(opt)
            if len(options) >= req.max_options:
                break
        if not options:
            raise no_candidates(by_line())

    return {"trace_id": trace_id, "options": options, "excluded": by_line()}


def _assemble(
    req: OfferRequest, filled: list[list[FilledLine]], mandate: Mandate, agent_id: str,
    customer_id: str, now: datetime, settings: Any, refs: list[LineRef] | None = None,
    ttl_seconds: int | None = None,
) -> Offer:
    lines: list[OfferLine] = []
    fulfilment: list[FulfilmentSummary] = []
    for i, (line, fls) in enumerate(zip(req.lines, filled, strict=True), start=1):
        unit = line.quantity.unit
        ref = refs[i - 1] if refs else None
        for fl in fls:
            lines.append(_offer_line(len(lines) + 1, fl, unit, ref))
        offered = sum(((fl.base if fl.base is not None else fl.units) for fl in fls), Decimal(0))
        fulfilment.append(FulfilmentSummary(
            request_line=i, requested=Quantity(value=line.quantity.value, unit=unit),
            offered=Quantity(value=_num(offered), unit=unit),
            complete=offered >= line.quantity.value,
            split=len({fl.prep.cand.product.product_id for fl in fls}) > 1,
        ))
    excl = sum((Decimal(ln.line_total_excl_tax) for ln in lines), Decimal(0))
    incl = sum((Decimal(ln.line_total_incl_tax) for ln in lines), Decimal(0))
    # ttl_seconds: internal override (the public demo's expiry scenario); not in the API.
    expires = now + timedelta(seconds=ttl_seconds or settings.offer_ttl_seconds)
    for ln in lines:
        if ln.availability.order_by is not None and ln.availability.order_by < expires:
            expires = ln.availability.order_by
    used_facts = [fl.prep.facts for fls in filled for fl in fls]
    price_at = min(f.price.fetched_at for f in used_facts if f.price)  # type: ignore[union-attr]
    avail_at = min(f.availability.fetched_at for f in used_facts if f.availability)  # type: ignore[union-attr]
    return Offer(
        offer_id=new_id("off"), status="active", issuer=settings.public_base_url,
        agent_id=agent_id, customer_id=customer_id, mandate_id=mandate.mandate_id,
        mandate_version=mandate.version, issued_at=now, expires_at=expires,
        delivery_date=req.delivery_date, lines=lines, fulfilment=fulfilment,
        totals=Totals(excl_tax=money(excl), tax=money(incl - excl), incl_tax=money(incl)),
        claims_source=ClaimsSource(
            price=ClaimSource(source="valio_aimo_price_api", fetched_at=price_at),
            availability=ClaimSource(source="valio_aimo_availability_api", fetched_at=avail_at),
            attributes=ClaimSource(source="gs1_synkka_via_valio_aimo",
                                   assurance="supplier_declared"),
        ),
    )


def _persist(
    conn: Connection, offer: Offer, payload: dict[str, Any], jws: str, req: OfferRequest,
    filled: list[list[FilledLine]], trace_id: str,
) -> None:
    """Offer row, lines, holds and `offer.issued`, in the caller's transaction."""
    db.q(conn,
         "INSERT INTO offers (offer_id, trace_id, agent_id, customer_id, mandate_id, "
         "mandate_version, status, option_label, delivery_date, currency, total_excl_tax, "
         "total_tax, total_incl_tax, issued_at, expires_at, request_json, payload_json, jws) "
         "VALUES (:o, :t, :a, :c, :m, :mv, 'active', 'best', :d, 'EUR', :te, :tt, :ti, :ia, "
         ":ea, :rq, :pl, :jws)",
         o=offer.offer_id, t=trace_id, a=offer.agent_id, c=offer.customer_id,
         m=offer.mandate_id, mv=offer.mandate_version, d=offer.delivery_date,
         te=offer.totals.excl_tax, tt=offer.totals.tax, ti=offer.totals.incl_tax,
         ia=timeutil.to_db(offer.issued_at), ea=timeutil.to_db(offer.expires_at),
         rq=db.jdump(req.model_dump(mode="json")), pl=db.jdump(payload), jws=jws)
    fls = [fl for group in filled for fl in group]
    for ln, fl in zip(offer.lines, fls, strict=True):
        f = fl.prep.facts
        db.q(conn,
             "INSERT INTO offer_lines (offer_id, line_no, request_line, product_id, role, "
             "substitute_for, substitution_source, quantity_sales_units, sales_unit, "
             "base_quantity, base_unit, unit_price_excl_tax, tax_rate_percent, "
             "line_total_excl_tax, line_total_incl_tax, logistic_model, assurance, "
             "price_quote_id, availability_snapshot_id) VALUES (:o, :n, :rl, :p, :r, :sf, "
             ":ss, :q, :su, :bq, :bu, :up, :tr, :le, :li, :lm, :as_, :pq, :av)",
             o=offer.offer_id, n=ln.line_no, rl=ln.request_line, p=ln.product_id, r=ln.role,
             sf=fl.prep.cand.substitute_for, ss=fl.prep.cand.source,
             q=fl.units, su=ln.sales_unit,
             bq=fl.base.quantize(_QTY) if fl.base is not None else None,
             bu=ln.base_quantity.unit if ln.base_quantity else None,
             up=fl.prep.unit_excl.quantize(_BASIS, ROUND_HALF_UP), tr=fl.prep.tax_rate,
             le=ln.line_total_excl_tax, li=ln.line_total_incl_tax,
             lm=ln.availability.logistic_model, as_=ln.availability.assurance,
             pq=(f.price.price_quote_id if f.price else None) or 0,
             av=(f.availability.availability_snapshot_id if f.availability else None) or 0)
        db.q(conn,
             "INSERT INTO stock_holds (offer_id, line_no, product_id, delivery_date, "
             "quantity_sales_units, status, expires_at) VALUES (:o, :n, :p, :d, :q, 'held', :e)",
             o=offer.offer_id, n=ln.line_no, p=ln.product_id, d=offer.delivery_date,
             q=fl.units, e=timeutil.to_db(offer.expires_at))
    what = ", ".join(
        f"{_qty_text(ln.base_quantity or ln.quantity)} of {ln.product_id}" for ln in offer.lines)
    until = offer.expires_at.astimezone(timeutil.HELSINKI).strftime("%H:%M")
    events.append(
        conn, trace_id=trace_id, type="offer.issued", actor_type="gateway", audience="both",
        text=(f"Issued offer {offer.offer_id} for {what}, total {offer.totals.excl_tax} EUR "
              f"excl. tax, valid until {until}."),
        body={"offer_id": offer.offer_id, "totals": offer.totals.dump(),
              "expires_at": timeutil.iso(offer.expires_at),
              "mandate_version": offer.mandate_version,
              "lines": [{"line_no": ln.line_no, "product_id": ln.product_id, "role": ln.role,
                         "quantity": ln.quantity.dump(),
                         "assurance": ln.availability.assurance} for ln in offer.lines],
              "fulfilment": [fs.dump() for fs in offer.fulfilment]},
    )


# ================================================================ reading and expiry


def load_offer(conn: Connection, offer_id: str, *, for_update: bool = False
               ) -> dict[str, Any] | None:
    """Offer row as a dict: status, expires_at (aware UTC), payload (dict), jws, trace_id..."""
    sql = "SELECT * FROM offers WHERE offer_id = :o" + (" FOR UPDATE" if for_update else "")
    row = db.q(conn, sql, o=offer_id).mappings().first()
    if row is None:
        return None
    d = dict(row)
    d["issued_at"] = timeutil.from_db(d["issued_at"])
    d["expires_at"] = timeutil.from_db(d["expires_at"])
    d["payload"] = db.jload(d.pop("payload_json"))
    d["request"] = db.jload(d.pop("request_json"))
    return d


# ================================================================ ranking (document 05, 4; T-501)

CRITERIA = ("price", "delivery_reliability", "carbon", "labels")


def _unit_price(p: Prepared) -> Decimal:
    return p.unit_excl / p.per_unit


def line_scores(prepared: list[Prepared], mandate: Mandate) -> dict[str, dict[str, Any]]:
    """Score per candidate of one line (document 05, section 4): each criterion's weighted
    score over the criteria that have a value, divided by the sum of those weights."""
    weights = mandate.preferences.weights
    preferred = list(mandate.preferences.preferred_labels or [])
    prices = [_unit_price(p) for p in prepared]
    carbons = [p.cand.product.carbon_kg_co2e_per_kg for p in prepared
               if p.cand.product.carbon_kg_co2e_per_kg]
    low_price = min(prices) if prices else None
    low_carbon = min(carbons) if carbons else None
    out: dict[str, dict[str, Any]] = {}
    for p in prepared:
        prod = p.cand.product
        raw: dict[str, float] = {}
        if low_price is not None and _unit_price(p) > 0:
            raw["price"] = float(low_price / _unit_price(p))
        if prod.delivery_reliability is not None:
            raw["delivery_reliability"] = float(prod.delivery_reliability)
        if low_carbon and prod.carbon_kg_co2e_per_kg:
            raw["carbon"] = float(low_carbon) / float(prod.carbon_kg_co2e_per_kg)
        raw["labels"] = (len(set(prod.labels) & set(preferred)) / len(preferred)
                         if preferred else 0.0)
        wsum = sum(weights.get(k, 0.0) for k in raw)
        breakdown = {k: (weights.get(k, 0.0) * v / wsum if wsum else 0.0) for k, v in raw.items()}
        out[prod.product_id] = {"total": sum(breakdown.values()), "breakdown": breakdown}
    return out


def ranked_variants(prepared: list[list[Prepared]], scores: list[dict[str, dict[str, Any]]]
                    ) -> list[tuple[str, list[list[Prepared]]]]:
    """best: by descending score; better: cheapest first; good: fewest distinct products
    (candidates that can fill the line alone first). Listed candidates stay before the
    gateway's substitutes in every option, so ranking never adds a substitution."""
    def sub(p: Prepared) -> int:
        return 1 if p.cand.role == "substitute" else 0

    best = [sorted(ok, key=lambda p, sc=sc: (sub(p), -sc[p.cand.product.product_id]["total"],
                                              p.cand.pos)) for ok, sc in zip(prepared, scores)]
    better = [sorted(ok, key=lowest_price_key) for ok in prepared]
    good = [sorted(ok, key=lambda p, sc=sc: (sub(p), 0 if p.capacity is None else 1,
                                              -(p.capacity or 0),
                                              -sc[p.cand.product.product_id]["total"], p.cand.pos))
            for ok, sc in zip(prepared, scores)]
    return [("best", best), ("better", better), ("good", good)]


def option_score(filled: list[list[FilledLine]], scores: list[dict[str, dict[str, Any]]]
                 ) -> dict[str, Any]:
    """An option's score: its products' scores weighted by their share of the option's total
    excl. tax. The breakdown lists each criterion's weighted part."""
    parts: list[tuple[Decimal, dict[str, Any]]] = []
    for fls, sc in zip(filled, scores):
        for fl in fls:
            parts.append((fl.units * fl.prep.unit_excl, sc[fl.prep.cand.product.product_id]))
    total_w = sum((w for w, _ in parts), Decimal(0)) or Decimal(1)
    breakdown: dict[str, float] = {}
    for w, s in parts:
        for k, v in s["breakdown"].items():
            breakdown[k] = breakdown.get(k, 0.0) + float(w / total_w) * v
    return {"total": round(sum(breakdown.values()), 4),
            "breakdown": {k: round(v, 4) for k, v in breakdown.items()}}


def lowest_price_key(p: Prepared) -> tuple[int, Decimal, int]:
    """`prefer: lowest_price`: listed candidates (requested, alternates) before substitutes,
    each group by price excl. tax per requested unit (per L or KG after conversion, else per
    sales unit), ties in the agent's order."""
    return (1 if p.cand.role == "substitute" else 0, p.unit_excl / p.per_unit, p.cand.pos)


def withdraw_offer(offer_id: str, agent_id: str) -> None:
    """`replaces_offer_id`: withdraw the agent's own active offer and release its holds.

    An offer of another agent is reported as not found; an offer that is no longer active
    (consumed or expired) is left as it is."""
    with db.transaction() as conn:
        row = load_offer(conn, offer_id, for_update=True)
        if row is None or row["agent_id"] != agent_id:
            raise HandshakeError("not_found", f"Offer {offer_id} does not exist.")
        row = expire_if_due(conn, row)
        if row["status"] != "active":
            return
        db.q(conn, "UPDATE offers SET status = 'expired' WHERE offer_id = :o "
                   "AND status = 'active'", o=offer_id)
        events.append(
            conn, trace_id=row["trace_id"], type="offer.expired", actor_type="gateway",
            audience="both", text=f"Offer {offer_id} was withdrawn: the agent replaced it with "
                                  f"a new request.",
            body={"offer_id": offer_id, "reason": "replaced"},
        )
        release_holds(conn, offer_id, row["trace_id"], "offer replaced")


def release_holds(conn: Connection, offer_id: str, trace_id: str, why: str) -> list[dict]:
    """Release the offer's `held` holds and write `holds.changed` (aimo_only) if any."""
    rows = db.q(conn,
                "SELECT hold_id, line_no, product_id, quantity_sales_units FROM stock_holds "
                "WHERE offer_id = :o AND status = 'held' FOR UPDATE", o=offer_id).mappings().all()
    if not rows:
        return []
    db.q(conn, "UPDATE stock_holds SET status = 'released' WHERE offer_id = :o "
               "AND status = 'held'", o=offer_id)
    released = [{"line_no": r["line_no"], "product_id": r["product_id"],
                 "quantity_sales_units": str(_num(Decimal(r["quantity_sales_units"])))}
                for r in rows]
    events.append(
        conn, trace_id=trace_id, type="holds.changed", actor_type="gateway",
        audience="aimo_only",
        text=f"Released {len(rows)} hold(s) of offer {offer_id} ({why}).",
        body={"offer_id": offer_id, "status": "released", "holds": released},
    )
    return released


def expire_if_due(conn: Connection, offer_row: dict[str, Any]) -> dict[str, Any]:
    """Lazy expiry (A-12). Call inside a transaction, ideally on a row read FOR UPDATE.

    If the offer is `active` and `expires_at <= now`: set it `expired`, release its holds,
    write `offer.expired` (both) and `holds.changed` (aimo_only). Returns the (updated) row.
    """
    if offer_row["status"] != "active" or offer_row["expires_at"] > timeutil.now():
        return offer_row
    n = db.q(conn, "UPDATE offers SET status = 'expired' WHERE offer_id = :o "
                   "AND status = 'active'", o=offer_row["offer_id"]).rowcount
    if n:
        oid, trace = offer_row["offer_id"], offer_row["trace_id"]
        at = timeutil.iso(offer_row["expires_at"])
        events.append(
            conn, trace_id=trace, type="offer.expired", actor_type="gateway", audience="both",
            text=f"Offer {oid} expired at {at} without an order.",
            body={"offer_id": oid, "expires_at": at},
        )
        release_holds(conn, oid, trace, "offer expired")
    return {**offer_row, "status": "expired"}


def get_offer_view(offer_id: str, agent_id: str) -> dict[str, Any]:
    """GET /v1/offers/{offer_id}: {"offer": payload with current status, "jws": ...}."""
    with db.transaction() as conn:
        row = load_offer(conn, offer_id, for_update=True)
        if row is None:
            raise HandshakeError("not_found", f"Offer {offer_id} does not exist.")
        if row["agent_id"] != agent_id:
            raise forbidden()
        row = expire_if_due(conn, row)
    payload = dict(row["payload"])
    payload["status"] = row["status"]
    return {"offer": payload, "jws": row["jws"]}


__all__ = [
    "ALLERGEN_NAMES", "CatalogProduct", "build_offer", "convert", "expire_if_due",
    "get_offer_view", "held_quantities", "load_catalog", "load_offer",
    "mandate_product_reasons", "price_str", "release_holds", "round_down_to_step",
    "differences", "sales_units_needed", "substitute_ids", "validate_request",
]
