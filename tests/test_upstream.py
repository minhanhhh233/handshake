"""Upstream adapter (task T-104; document 06, sections 1-2). Never calls the live API."""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from handshake import db, events, timeutil
from handshake.config import get_settings
from handshake.errors import HandshakeError
from handshake.upstream import UpstreamAdapter, normalise_availability, normalise_price

FIXTURES = Path(__file__).parent / "fixtures" / "upstream"
PRICE_FIX = FIXTURES / "price_2026-11-16.json"
AVAIL_FIX = FIXTURES / "availability_2026-11-16.json"
SECRET = "sk-test-NEVER-LOG-ME-7f3a91"
D = date(2026, 11, 16)
T0 = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)


def _load(path: Path) -> dict:
    if not path.exists():
        pytest.skip(f"recorded fixture {path.name} not present")
    return json.loads(path.read_text(encoding="utf-8"))["response"]


# ---------------------------------------------------------------- synthetic answers


def price_obj(pid: str, value: float | None = 1.5, *, error: bool = False) -> dict:
    cat = None if value is None else {
        "value": value, "valueWithTax": round(value * 1.135, 2),
        "validFrom": "2026-09-01T00:00:00+0300", "validTo": "2026-12-31T00:00:00+0200",
        "currency": "EUR"}
    return {
        "productId": pid, "quantity": 1.0, "catalogPrice": cat,
        "comparisonPrice": None if value is None else {"value": value, "valueWithTax": 0},
        "comparisonUnit": "L", "tax": {"taxRate": 13.5, "taxValue": 0.2},
        "netPriceFound": False, "error": error,
    }


def avail_obj(pid: str, *, model: str = "Stock", amount: float = 100.0, lines: bool = True,
              block: bool = False, line_block: bool = False) -> dict:
    return {
        "productId": pid, "closingTime": "2026-11-15T17:00:00+0200", "salesBlock": block,
        "logisticModel": model,
        "scheduleLines": [{"salesBlock": line_block, "availableAmount": amount,
                           "availableDate": "2026-11-16", "salesUnit": "ST"}] if lines else [],
    }


class Upstream:
    """MockTransport handler that answers from per-product dicts and records requests."""

    def __init__(self, prices=None, avails=None, script=None):
        self.prices = prices or {}
        self.avails = avails or {}
        self.script = list(script or [])  # per-request overrides: status int or exception
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            if step != 200:
                return httpx.Response(step, json={"detail": "nope"})
        body = json.loads(request.content)
        ids = body["products"]
        if request.url.path == "/api/price":
            prods = [self.prices[p] for p in ids if p in self.prices]
            return httpx.Response(200, json={"customerId": "123", "currency": "EUR",
                                             "pricingDate": "x", "products": prods})
        prods = [self.avails[p] for p in ids if p in self.avails]
        return httpx.Response(200, json={"timeNow": "2026-11-14T00:11:49+0200",
                                         "requestedDate": "x", "products": prods})


def make_adapter(handler, **kw) -> UpstreamAdapter:
    settings = dataclasses.replace(
        get_settings(), valio_api_key=SECRET, valio_api_base="https://upstream.test",
        upstream_cache_ttl_seconds=60, upstream_timeout_seconds=1.0)
    return UpstreamAdapter(transport=httpx.MockTransport(handler), settings=settings,
                           sleep=lambda s: None, **kw)


def standard(ids=("A1", "A2", "A3")) -> Upstream:
    return Upstream({p: price_obj(p) for p in ids}, {p: avail_obj(p) for p in ids})


# ---------------------------------------------------------------- normalisation: recorded


def _by_id(resp: dict) -> dict[str, dict]:
    return {p["productId"]: p for p in resp["products"]}


def test_recorded_availability_normalisation():
    av = _by_id(_load(AVAIL_FIX))
    a = normalise_availability(av["464306"], T0)
    assert a.logistic_model == "stock"
    assert a.available_amount is None  # 9999999999 -> unknown (Q-15)
    assert a.sales_block is False and a.has_schedule is True
    assert a.available_date == date(2026, 11, 16)
    assert a.closing_time is not None and a.closing_time.utcoffset() == timedelta(hours=2)
    assert a.closing_time == datetime(2026, 11, 15, 15, 0, tzinfo=timezone.utc)
    assert normalise_availability(av["469363"], T0).available_amount == Decimal("384")
    t = normalise_availability(av["465039"], T0)
    assert t.logistic_model == "terminal" and t.available_amount is None


def test_recorded_price_normalisation():
    pr = _by_id(_load(PRICE_FIX))
    p = normalise_price(pr["464306"], T0)
    assert p.unit_price_excl_tax == Decimal("1.01")
    assert p.tax_rate_percent == Decimal("13.5")
    assert p.comparison_unit == "L" and p.currency == "EUR"
    assert p.valid_from and "+0" in p.valid_from  # stored as received
    assert p.error is False
    p2 = normalise_price(pr["464398"], T0)
    assert p2.unit_price_excl_tax == Decimal("1.65")
    assert p2.unit_price_incl_tax == Decimal("1.87")
    assert p2.net_price_found is True


# ---------------------------------------------------------------- normalisation: synthetic


def test_empty_schedule_lines():
    a = normalise_availability(avail_obj("X", lines=False), T0)
    assert a.has_schedule is False and a.available_date is None and a.available_amount is None


def test_sales_block_product_and_line_level():
    assert normalise_availability(avail_obj("X", block=True), T0).sales_block is True
    assert normalise_availability(avail_obj("X", line_block=True), T0).sales_block is True
    assert normalise_availability(avail_obj("X"), T0).sales_block is False


def test_null_catalog_price_and_error():
    p = normalise_price(price_obj("X", None), T0)
    assert p.unit_price_excl_tax is None and p.unit_price_incl_tax is None
    assert p.comparison_price is None
    raw = price_obj("X")
    raw["tax"] = None
    assert normalise_price(raw, T0).tax_rate_percent is None
    assert normalise_price(price_obj("X", error=True), T0).error is True


def test_logistic_model_normalisation():
    assert normalise_availability(avail_obj("X", model="Drop ship"), T0).logistic_model == "unknown"
    assert normalise_availability(avail_obj("X", model="STOCK_DC"), T0).logistic_model == "stock"
    assert normalise_availability(avail_obj("X", model="terminal"), T0).logistic_model == "terminal"
    t = normalise_availability(avail_obj("X", model="Terminal", amount=999999.0), T0)
    assert t.available_amount is None


# ---------------------------------------------------------------- adapter (DB)


@pytest.mark.db
def test_fetch_missing_product_and_request_shape(clean_db):
    up = standard(("A1", "A2"))
    facts = make_adapter(up).get_facts(["A1", "A2", "ZZ"], D, trace_id="trc_u1")
    assert set(facts) == {"A1", "A2", "ZZ"}
    assert facts["ZZ"].price is None and facts["ZZ"].availability is None
    assert facts["A1"].price.price_quote_id and facts["A1"].availability.availability_snapshot_id
    assert len(up.requests) == 2
    paths = sorted(r.url.path for r in up.requests)
    assert paths == ["/api/availability", "/api/price"]
    for r in up.requests:
        assert r.headers["X-API-Key"] == SECRET
        body = json.loads(r.content)
        assert body["requestedDate"] == "2026-11-16" and body["products"] == ["A1", "A2", "ZZ"]
        assert body["customer"] == get_settings().valio_customer


@pytest.mark.db
def test_chunking_at_1000(clean_db):
    ids = [f"P{i}" for i in range(1500)]
    up = Upstream()
    facts = make_adapter(up).get_facts(ids, D, trace_id="trc_chunk")
    assert len(facts) == 1500
    sizes = sorted(len(json.loads(r.content)["products"]) for r in up.requests)
    assert sizes == [500, 500, 1000, 1000]


@pytest.mark.db
def test_cache_hit_and_expiry(clean_db):
    up = standard()
    ad = make_adapter(up)
    first = ad.get_facts(["A1", "A2"], D, trace_id="trc_c")
    assert len(up.requests) == 2
    second = ad.get_facts(["A1", "A2"], D, trace_id="trc_c")
    assert len(up.requests) == 2  # served from cache
    for pid in ("A1", "A2"):
        assert second[pid].price.price_quote_id == first[pid].price.price_quote_id
        assert (second[pid].availability.availability_snapshot_id
                == first[pid].availability.availability_snapshot_id)
    # partial hit: only the miss goes upstream
    ad.get_facts(["A1", "A3"], D, trace_id="trc_c")
    assert [json.loads(r.content)["products"] for r in up.requests[2:]] == [["A3"], ["A3"]]
    # other delivery date is a different key
    ad.get_facts(["A1"], date(2026, 11, 17), trace_id="trc_c")
    assert len(up.requests) == 6
    timeutil.shift_clock(timedelta(seconds=61))
    third = ad.get_facts(["A1", "A2"], D, trace_id="trc_c")
    assert len(up.requests) == 8
    assert third["A1"].price.price_quote_id != first["A1"].price.price_quote_id


@pytest.mark.db
def test_retry_then_success(clean_db):
    up = standard()
    up.script = [503]  # first request fails, everything after succeeds
    facts = make_adapter(up).get_facts(["A1"], D, trace_id="trc_r")
    assert facts["A1"].price is not None
    assert len(up.requests) == 3  # one endpoint retried once, the other succeeded at once


def _single_endpoint(script):
    """Handler where only /api/price follows `script`; availability always succeeds."""
    up = standard()
    price_script = list(script)
    base = up.__call__

    def handler(request):
        if request.url.path == "/api/price" and price_script:
            up.requests.append(request)
            step = price_script.pop(0)
            if isinstance(step, Exception):
                raise step
            if step != 200:
                return httpx.Response(step)
            up.requests.pop()
        return base(request)
    return up, handler


@pytest.mark.db
def test_retry_exactly_two_price_requests(clean_db):
    up, handler = _single_endpoint([503, 200])
    make_adapter(handler).get_facts(["A1"], D, trace_id="trc_r2")
    assert sum(r.url.path == "/api/price" for r in up.requests) == 2


@pytest.mark.db
@pytest.mark.parametrize("step", [503, 429, 500])
def test_two_failures_raise(clean_db, step):
    up, handler = _single_endpoint([step, step])
    with pytest.raises(HandshakeError) as ei:
        make_adapter(handler).get_facts(["A1"], D, trace_id="trc_f")
    assert ei.value.code == "upstream_unavailable" and ei.value.status == 503
    assert sum(r.url.path == "/api/price" for r in up.requests) == 2


@pytest.mark.db
def test_timeout_raises_upstream_unavailable(clean_db):
    up, handler = _single_endpoint([httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")])
    with pytest.raises(HandshakeError) as ei:
        make_adapter(handler).get_facts(["A1"], D, trace_id="trc_t")
    assert ei.value.code == "upstream_unavailable"
    assert sum(r.url.path == "/api/price" for r in up.requests) == 2


@pytest.mark.db
def test_4xx_not_retried(clean_db):
    up, handler = _single_endpoint([422])
    with pytest.raises(HandshakeError) as ei:
        make_adapter(handler).get_facts(["A1"], D, trace_id="trc_4")
    assert ei.value.code == "upstream_unavailable"
    assert sum(r.url.path == "/api/price" for r in up.requests) == 1


@pytest.mark.db
def test_failure_is_not_cached(clean_db):
    up, handler = _single_endpoint([503, 503])
    ad = make_adapter(handler)
    with pytest.raises(HandshakeError):
        ad.get_facts(["A1"], D, trace_id="trc_nc")
    facts = ad.get_facts(["A1"], D, trace_id="trc_nc")
    assert facts["A1"].price is not None


@pytest.mark.db
def test_snapshots_event_and_no_secret(clean_db, caplog):
    caplog.set_level(logging.DEBUG)
    up = standard(("A1", "A2", "A3"))
    up.avails["A2"] = avail_obj("A2", model="Terminal", amount=999999.0)
    up.script = [503]  # exercise the warning log path too
    ad = make_adapter(up)
    f1 = ad.get_facts(["A1", "A2"], D, trace_id="trc_s", actor_id="agt_1")
    ad.get_facts(["A1", "A2", "A3"], D, trace_id="trc_s", actor_id="agt_1")

    with db.connection() as conn:
        pq = db.q(conn, "SELECT * FROM price_quotes ORDER BY price_quote_id").mappings().all()
        av = db.q(conn, "SELECT * FROM availability_snapshots ORDER BY"
                        " availability_snapshot_id").mappings().all()
        evs = events.list_events(conn, "trc_s")
    assert [r["product_id"] for r in pq] == ["A1", "A2", "A3"]
    assert [r["product_id"] for r in av] == ["A1", "A2", "A3"]
    a1 = next(r for r in av if r["product_id"] == "A1")
    assert a1["closing_time"] == "2026-11-15T17:00:00+0200"
    assert a1["available_date"] == "2026-11-16" and a1["logistic_model"] == "Stock"
    assert a1["upstream_time_now"] == "2026-11-14T00:11:49+0200"
    assert a1["availability_snapshot_id"] == f1["A1"].availability.availability_snapshot_id
    assert db.jload(a1["raw_json"])["productId"] == "A1"
    p1 = next(r for r in pq if r["product_id"] == "A1")
    assert p1["valid_from"] == "2026-09-01T00:00:00+0300"
    assert p1["unit_price_excl_tax"] == Decimal("1.5") and p1["pricing_date"] == D

    assert [e["type"] for e in evs] == ["upstream.queried", "upstream.queried"]
    assert all(e["actor_type"] == "gateway" and e["audience"] == "aimo_only" for e in evs)
    b2 = evs[1]["body"]
    assert b2["fetched"] == ["A3"] and b2["cache_hit"] == ["A1", "A2"]
    assert b2["product_ids"] == ["A1", "A2", "A3"]
    assert b2["price_quote_ids"]["A1"] == f1["A1"].price.price_quote_id
    assert "1 product" in evs[1]["text"] and "2 served from cache" in evs[1]["text"]
    assert evs[0]["text"].startswith("Fetched price and availability for 2 products")

    assert any("503" in r.getMessage() for r in caplog.records)
    assert SECRET not in caplog.text
    with db.connection() as conn:
        for table in ("price_quotes", "availability_snapshots", "events"):
            rows = db.q(conn, f"SELECT * FROM {table}").all()
            assert SECRET not in repr(rows)


@pytest.mark.db
def test_recorded_fixture_end_to_end(clean_db):
    pr, av = _load(PRICE_FIX), _load(AVAIL_FIX)

    def handler(request):
        return httpx.Response(200, json=pr if request.url.path == "/api/price" else av)
    ids = [p["productId"] for p in pr["products"]]
    facts = make_adapter(handler).get_facts(ids, D, trace_id="trc_fx")
    assert facts["464306"].price.unit_price_excl_tax == Decimal("1.01")
    assert facts["464306"].availability.available_amount is None
    assert facts["469363"].availability.available_amount == Decimal("384")
    with db.connection() as conn:
        amt = db.q(conn, "SELECT available_amount FROM availability_snapshots"
                         " WHERE product_id = '464306'").scalar_one()
    assert amt == Decimal("9999999999")
