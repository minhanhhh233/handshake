"""Upstream fakes. Tests never call the live Valio Aimo APIs (document 09, section 1)."""

from __future__ import annotations

import itertools
from datetime import date, datetime
from decimal import Decimal

from handshake import timeutil
from handshake.models import UpstreamAvailability, UpstreamFacts, UpstreamPrice

_ids = itertools.count(1)


def make_facts(
    product_id: str,
    *,
    price: str | None = "1.01",
    comparison: str | None = "1.01",
    comparison_unit: str | None = "L",
    tax: str = "13.5",
    error: bool = False,
    logistic: str = "stock",
    amount: str | None = "9999",
    available_date: date = date(2026, 11, 16),
    closing: datetime | None = None,
    sales_block: bool = False,
    has_schedule: bool = True,
    sales_unit: str = "ST",
    valid_from: str | None = "2026-09-01T00:00:00+0300",
    valid_to: str | None = "2026-12-31T00:00:00+0200",
) -> UpstreamFacts:
    """One product's normalised upstream facts. `amount=None` means unknown (>= 999999)."""
    now = timeutil.now()
    closing = closing or datetime(2026, 11, 15, 17, 0, tzinfo=timeutil.HELSINKI)
    p = Decimal(price) if price is not None else None
    t = Decimal(tax)
    return UpstreamFacts(
        product_id=product_id,
        price=UpstreamPrice(
            product_id=product_id, price_quote_id=next(_ids), fetched_at=now, currency="EUR",
            unit_price_excl_tax=p,
            unit_price_incl_tax=(p * (1 + t / 100)).quantize(Decimal("0.01")) if p else None,
            comparison_price=Decimal(comparison) if comparison is not None else None,
            comparison_unit=comparison_unit, tax_rate_percent=t, valid_from=valid_from,
            valid_to=valid_to, net_price_found=True, error=error,
        ),
        availability=UpstreamAvailability(
            product_id=product_id, availability_snapshot_id=next(_ids), fetched_at=now,
            closing_time=closing, sales_block=sales_block, logistic_model=logistic,
            available_amount=Decimal(amount) if amount is not None else None,
            available_date=available_date if has_schedule else None,
            sales_unit=sales_unit, has_schedule=has_schedule,
        ),
    )


class FakeUpstream:
    """Drop-in for `handshake.upstream.UpstreamAdapter`."""

    def __init__(self, facts: list[UpstreamFacts] | None = None, fail: Exception | None = None):
        self.facts = {f.product_id: f for f in facts or []}
        self.fail = fail
        self.calls: list[tuple[list[str], date]] = []

    def get_facts(self, product_ids, delivery_date, *, trace_id, actor_id=None):  # noqa: ANN001
        self.calls.append((list(product_ids), delivery_date))
        if self.fail:
            raise self.fail
        return {
            pid: self.facts.get(pid, UpstreamFacts(product_id=pid, price=None, availability=None))
            for pid in product_ids
        }
