"""Upstream adapter for Valio Aimo's /api/price and /api/availability (task T-104; doc 06, 1-2).

    adapter = get_adapter()
    facts = adapter.get_facts(product_ids, delivery_date, trace_id=..., actor_id=...)
    # -> dict[product_id, UpstreamFacts]; every requested ID is a key.
    #    price / availability is None when upstream did not return that product.
    # Raises HandshakeError("upstream_unavailable") on timeout, transport error, 5xx or 429
    # after one retry, and on any other non-2xx answer.

Behaviour (document 06, section 2):

- In-process cache keyed (product_id, delivery_date), lifetime UPSTREAM_CACHE_TTL_SECONDS,
  measured with `timeutil.now()` so tests can move the clock. Entries keep the normalised
  facts including their snapshot IDs; nothing older than the lifetime is ever served.
- Cache misses go upstream as one price call and one availability call (chunked at 1,000 IDs),
  issued concurrently.
- Every fetched product gets one `price_quotes` and one `availability_snapshots` row, and each
  `get_facts` call writes one `upstream.queried` event (actor gateway, audience aimo_only), in
  its own transaction so the snapshots persist even if the offer later fails.
- The API key is sent only in the X-API-Key header. It is never logged or stored.

Tests replace the adapter with `set_adapter(FakeUpstream(...))` (tests/fakes.py).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from . import db, events, timeutil
from .config import Settings, get_settings, tls_context
from .errors import upstream_unavailable
from .models import UpstreamAvailability, UpstreamFacts, UpstreamPrice

log = logging.getLogger(__name__)

CHUNK = 1000
UNKNOWN_AMOUNT = Decimal("999999")  # >= this means unknown quantity (Terminal sentinel, Q-15)
RETRY_STATUSES = {429}


# ---------------------------------------------------------------- normalisation (doc 06, 1.3/1.4)


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def normalise_price(raw_product: dict[str, Any], fetched_at: datetime) -> UpstreamPrice:
    """One product object of the `/api/price` answer to `UpstreamPrice` (no I/O)."""
    catalog = _obj(raw_product.get("catalogPrice"))
    comparison = _obj(raw_product.get("comparisonPrice"))
    tax = _obj(raw_product.get("tax"))
    return UpstreamPrice(
        product_id=str(raw_product.get("productId")),
        fetched_at=fetched_at,
        currency=_str(catalog.get("currency")),
        unit_price_excl_tax=_dec(catalog.get("value")),
        unit_price_incl_tax=_dec(catalog.get("valueWithTax")),
        comparison_price=_dec(comparison.get("value")),
        comparison_unit=_str(raw_product.get("comparisonUnit")) if comparison else None,
        tax_rate_percent=_dec(tax.get("taxRate")),
        valid_from=_str(catalog.get("validFrom")),
        valid_to=_str(catalog.get("validTo")),
        net_price_found=bool(raw_product.get("netPriceFound")),
        error=bool(raw_product.get("error")),
    )


def _logistic_model(value: Any) -> str:
    s = str(value or "").lower()
    if "stock" in s:
        return "stock"
    if "terminal" in s:
        return "terminal"
    return "unknown"


def normalise_availability(
    raw_product: dict[str, Any], fetched_at: datetime
) -> UpstreamAvailability:
    """One product object of the `/api/availability` answer to `UpstreamAvailability`."""
    lines = [ln for ln in (raw_product.get("scheduleLines") or []) if isinstance(ln, dict)]
    line = lines[0] if lines else {}
    amount = _dec(line.get("availableAmount")) if lines else None
    if amount is not None and amount >= UNKNOWN_AMOUNT:
        amount = None  # unknown quantity, no fill limit (Q-15)
    return UpstreamAvailability(
        product_id=str(raw_product.get("productId")),
        fetched_at=fetched_at,
        closing_time=timeutil.parse_upstream(raw_product.get("closingTime")),
        sales_block=bool(raw_product.get("salesBlock")) or any(
            bool(ln.get("salesBlock")) for ln in lines),
        logistic_model=_logistic_model(raw_product.get("logisticModel")),  # type: ignore[arg-type]
        available_amount=amount,
        available_date=timeutil.parse_upstream_date(line.get("availableDate")) if lines else None,
        sales_unit=_str(line.get("salesUnit")),
        has_schedule=bool(lines),
    )


# ---------------------------------------------------------------- adapter


@dataclass
class _Entry:
    stored_at: datetime
    facts: UpstreamFacts


class UpstreamAdapter:
    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        settings: Settings | None = None,
        clock: Callable[[], datetime] | None = None,
        retry_delay: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings or get_settings()
        s = self._settings
        if client is None:
            kw: dict[str, Any] = {"transport": transport} if transport is not None else {}
            client = httpx.Client(
                base_url=s.valio_api_base,
                headers={"X-API-Key": s.valio_api_key, "Accept": "application/json"},
                timeout=s.upstream_timeout_seconds,
                verify=tls_context(),
                **kw,
            )
        self._client = client
        self._clock = clock or timeutil.now
        self._retry_delay = retry_delay
        self._sleep = sleep
        self._cache: dict[tuple[str, date], _Entry] = {}
        self._lock = threading.Lock()

    # -------------------------------------------------- public

    def get_facts(
        self, product_ids: list[str], delivery_date: date, *, trace_id: str,
        actor_id: str | None = None,
    ) -> dict[str, UpstreamFacts]:
        ids = list(dict.fromkeys(str(p) for p in product_ids))
        ttl = timedelta(seconds=self._settings.upstream_cache_ttl_seconds)
        now = self._clock()

        result: dict[str, UpstreamFacts] = {}
        with self._lock:
            for pid in ids:
                e = self._cache.get((pid, delivery_date))
                if e is not None and now - e.stored_at < ttl and e.stored_at <= now:
                    result[pid] = e.facts
                elif e is not None:
                    del self._cache[(pid, delivery_date)]
        hits = [p for p in ids if p in result]
        misses = [p for p in ids if p not in result]

        fetched: dict[str, UpstreamFacts] = {}
        raw_prices: dict[str, dict] = {}
        raw_avail: dict[str, dict] = {}
        price_meta: dict[str, Any] = {}
        avail_meta: dict[str, Any] = {}
        fetched_at = now
        if misses:
            body = {
                "customer": self._settings.valio_customer,
                "requestedDate": delivery_date.isoformat(),
            }
            with ThreadPoolExecutor(max_workers=2) as pool:
                fp = pool.submit(self._fetch_all, "/api/price", body, misses)
                fa = pool.submit(self._fetch_all, "/api/availability", body, misses)
                price_answers, avail_answers = fp.result(), fa.result()
            fetched_at = self._clock()
            for ans in price_answers:
                price_meta = {k: v for k, v in ans.items() if k != "products"} or price_meta
                for p in ans.get("products") or []:
                    if isinstance(p, dict) and str(p.get("productId")) in misses:
                        raw_prices.setdefault(str(p["productId"]), p)
            for ans in avail_answers:
                avail_meta = {k: v for k, v in ans.items() if k != "products"} or avail_meta
                for p in ans.get("products") or []:
                    if isinstance(p, dict) and str(p.get("productId")) in misses:
                        raw_avail.setdefault(str(p["productId"]), p)
            for pid in misses:
                price = None
                if pid in raw_prices:
                    price = normalise_price(raw_prices[pid], fetched_at)
                    if price.currency is None and price_meta.get("currency"):
                        price.currency = str(price_meta["currency"])
                avail = (normalise_availability(raw_avail[pid], fetched_at)
                         if pid in raw_avail else None)
                fetched[pid] = UpstreamFacts(product_id=pid, price=price, availability=avail)

        self._persist(trace_id, actor_id, delivery_date, ids, hits, fetched, raw_prices,
                      raw_avail, avail_meta, fetched_at, result)

        if fetched:
            with self._lock:
                for pid, f in fetched.items():
                    self._cache[(pid, delivery_date)] = _Entry(stored_at=fetched_at, facts=f)
        result.update(fetched)
        # Copies, so a caller that edits a result cannot change what the cache serves.
        return {pid: result[pid].model_copy(deep=True) for pid in ids}

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # -------------------------------------------------- HTTP

    def _fetch_all(self, path: str, body: dict[str, Any], ids: list[str]) -> list[dict]:
        return [self._post(path, {**body, "products": ids[i:i + CHUNK]})
                for i in range(0, len(ids), CHUNK)]

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in (1, 2):
            retryable = False
            try:
                resp = self._client.post(path, json=body)
            except httpx.TimeoutException:
                log.warning("upstream %s timed out (attempt %d)", path, attempt)
                retryable = True
            except httpx.TransportError as exc:
                log.warning("upstream %s transport error %s (attempt %d)", path,
                            type(exc).__name__, attempt)
                retryable = True
            else:
                status = resp.status_code
                if 200 <= status < 300:
                    try:
                        data = resp.json()
                    except ValueError:
                        log.warning("upstream %s returned invalid JSON", path)
                        raise upstream_unavailable("Valio Aimo API returned an invalid answer.")
                    if not isinstance(data, dict):
                        raise upstream_unavailable("Valio Aimo API returned an invalid answer.")
                    return data
                log.warning("upstream %s returned HTTP %d (attempt %d)", path, status, attempt)
                if status >= 500 or status in RETRY_STATUSES:
                    retryable = True
                else:
                    raise upstream_unavailable(f"Valio Aimo API answered HTTP {status}.")
            if retryable and attempt == 1:
                self._sleep(self._retry_delay)
        raise upstream_unavailable()

    # -------------------------------------------------- persistence

    def _persist(
        self, trace_id: str, actor_id: str | None, delivery_date: date, ids: list[str],
        hits: list[str], fetched: dict[str, UpstreamFacts], raw_prices: dict[str, dict],
        raw_avail: dict[str, dict], avail_meta: dict[str, Any], fetched_at: datetime,
        cached: dict[str, UpstreamFacts],
    ) -> None:
        at_db = timeutil.to_db(fetched_at)
        with db.transaction() as conn:
            for pid, f in fetched.items():
                if f.price is not None:
                    p, raw = f.price, raw_prices[pid]
                    res = db.q(
                        conn,
                        "INSERT INTO price_quotes (product_id, pricing_date, fetched_at, currency,"
                        " unit_price_excl_tax, unit_price_incl_tax, comparison_price,"
                        " comparison_unit, tax_rate_percent, valid_from, valid_to,"
                        " net_price_found, error, raw_json) VALUES (:pid, :d, :at, :cur, :ex,"
                        " :inc, :cp, :cu, :tr, :vf, :vt, :npf, :err, :raw)",
                        pid=pid, d=delivery_date, at=at_db, cur=(p.currency or None),
                        ex=p.unit_price_excl_tax, inc=p.unit_price_incl_tax,
                        cp=p.comparison_price, cu=(p.comparison_unit or "")[:4] or None,
                        tr=p.tax_rate_percent, vf=_clip(p.valid_from), vt=_clip(p.valid_to),
                        npf=p.net_price_found, err=p.error, raw=db.jdump(raw),
                    )
                    p.price_quote_id = int(res.lastrowid)
                if f.availability is not None:
                    a, raw = f.availability, raw_avail[pid]
                    lines = [ln for ln in (raw.get("scheduleLines") or [])
                             if isinstance(ln, dict)]
                    line = lines[0] if lines else {}
                    res = db.q(
                        conn,
                        "INSERT INTO availability_snapshots (product_id, requested_date,"
                        " fetched_at, upstream_time_now, closing_time, sales_block,"
                        " logistic_model, available_amount, available_date, sales_unit,"
                        " raw_json) VALUES (:pid, :d, :at, :tn, :ct, :sb, :lm, :amt, :ad, :su,"
                        " :raw)",
                        pid=pid, d=delivery_date, at=at_db,
                        tn=_clip(avail_meta.get("timeNow")),
                        ct=_clip(raw.get("closingTime")), sb=a.sales_block,
                        lm=_clip(raw.get("logisticModel"), 16),
                        amt=_amount_for_db(line.get("availableAmount")),
                        ad=_clip(line.get("availableDate")),
                        su=_clip(line.get("salesUnit"), 8), raw=db.jdump(raw),
                    )
                    a.availability_snapshot_id = int(res.lastrowid)

            all_facts = {**cached, **fetched}
            missing = [p for p in ids if all_facts[p].price is None
                       and all_facts[p].availability is None]
            n_f, n_c = len(fetched), len(hits)
            text = (f"Fetched price and availability for {n_f} product{'s' * (n_f != 1)}; "
                    f"{n_c} served from cache.")
            if missing:
                text += f" Upstream returned nothing for {len(missing)}."
            events.append(
                conn, trace_id=trace_id, type="upstream.queried", actor_type="gateway",
                audience="aimo_only", text=text,
                body={
                    "delivery_date": delivery_date.isoformat(),
                    "product_ids": ids,
                    "fetched": list(fetched),
                    "cache_hit": hits,
                    "missing": missing,
                    "agent_id": actor_id,
                    "price_quote_ids": {
                        p: f.price.price_quote_id for p, f in all_facts.items() if f.price},
                    "availability_snapshot_ids": {
                        p: f.availability.availability_snapshot_id
                        for p, f in all_facts.items() if f.availability},
                },
            )


def _clip(value: Any, n: int = 40) -> str | None:
    if value is None:
        return None
    return str(value)[:n]


_DB_AMOUNT_MAX = Decimal("99999999999.999")  # DECIMAL(14,3)


def _amount_for_db(value: Any) -> Decimal | None:
    """Raw amount as received (9999999999 fits DECIMAL(14,3)); clamp anything larger."""
    d = _dec(value)
    if d is None:
        return None
    return max(min(d, _DB_AMOUNT_MAX), -_DB_AMOUNT_MAX)


_adapter: UpstreamAdapter | None = None


def get_adapter() -> UpstreamAdapter:
    global _adapter
    if _adapter is None:
        _adapter = UpstreamAdapter()
    return _adapter


def set_adapter(adapter: UpstreamAdapter | None) -> None:
    global _adapter
    _adapter = adapter
