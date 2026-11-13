"""Upstream adapter for Valio Aimo's /api/price and /api/availability (task T-104; doc 06, 1-2).

INTERFACE (shared contract; the implementation is task T-104):

    adapter = get_adapter()
    facts = adapter.get_facts(product_ids, delivery_date, trace_id=..., actor_id=...)
    # -> dict[product_id, UpstreamFacts]; every requested ID is a key.
    #    price / availability is None when upstream did not return that product.
    # Raises HandshakeError("upstream_unavailable") on timeout, 5xx or 429 after one retry.

Tests replace the adapter with `set_adapter(FakeUpstream(...))` (tests/fakes.py).
"""

from __future__ import annotations

from datetime import date

from .models import UpstreamFacts


class UpstreamAdapter:
    def get_facts(
        self, product_ids: list[str], delivery_date: date, *, trace_id: str,
        actor_id: str | None = None,
    ) -> dict[str, UpstreamFacts]:
        raise NotImplementedError("T-104")


_adapter: UpstreamAdapter | None = None


def get_adapter() -> UpstreamAdapter:
    global _adapter
    if _adapter is None:
        _adapter = UpstreamAdapter()
    return _adapter


def set_adapter(adapter: UpstreamAdapter | None) -> None:
    global _adapter
    _adapter = adapter
