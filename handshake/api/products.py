"""Product search and detail (document 03, section 3; tasks T-101 and T-102).

Authentication is optional. Static data only: never calls upstream (FR-02) and never returns
prices or availability (FR-05). Authenticated calls with a supplied X-Trace-Id write an
aimo_only event (document 05, section 7.1).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from .. import catalog, db, events
from ..models import ProductDetail, ProductList
from .deps import AgentContext, optional_agent

router = APIRouter(tags=["products"])


def _should_log(request: Request, agent: AgentContext | None) -> bool:
    return agent is not None and bool(getattr(request.state, "trace_supplied", False))


def _describe(filters: dict[str, Any]) -> str:
    parts = []
    for k, v in filters.items():
        if v in (None, [], ""):
            continue
        parts.append(f"{k} {','.join(v) if isinstance(v, list) else v}")
    return "; ".join(parts) or "no filters"


@router.get("/v1/products", response_model=ProductList, summary="Search products (static data)")
def search_products(
    request: Request,
    q: str | None = Query(default=None, description="Free text over English and Finnish names"),
    category: str | None = Query(default=None, description="Category code, e.g. 10400"),
    claims: str | None = Query(default=None, description="Comma list; product must have all"),
    exclude_allergens: str | None = Query(
        default=None, description="Comma list of GS1 codes; only explicit FREE_FROM passes"),
    labels: str | None = Query(default=None, description="Comma list; product must have all"),
    name_excludes: str | None = Query(
        default=None, description="Comma list; drop names containing any term"),
    limit: int = Query(default=catalog.DEFAULT_LIMIT, ge=1, le=catalog.MAX_LIMIT),
    cursor: str | None = Query(default=None, description="next_cursor from a previous page"),
    agent: AgentContext | None = Depends(optional_agent),  # noqa: B008
) -> dict[str, Any]:
    filters: dict[str, Any] = {
        "q": (q or "").strip() or None,
        "category": (category or "").strip() or None,
        "claims": catalog.split_list(claims),
        "exclude_allergens": catalog.split_list(exclude_allergens),
        "labels": catalog.split_list(labels),
        "name_excludes": catalog.split_list(name_excludes),
    }
    with db.connection() as conn:
        result = catalog.search(conn, limit=limit, cursor=cursor, **filters)
    if _should_log(request, agent):
        assert agent is not None
        with db.transaction() as conn:
            events.append(
                conn, trace_id=request.state.trace_id, type="search.performed",
                actor_type="agent", actor_id=agent.agent_id, audience="aimo_only",
                text=f"Agent searched products ({_describe(filters)}): "
                     f"{result.count} results on this page.",
                body={"filters": filters, "limit": limit, "paged": cursor is not None,
                      "result_count": result.count,
                      "product_ids": [i.product_id for i in result.items]},
            )
    return result.dump()


@router.get("/v1/products/{product_id}", response_model=ProductDetail,
            summary="Product detail (static data)")
def get_product(
    product_id: str,
    request: Request,
    agent: AgentContext | None = Depends(optional_agent),  # noqa: B008
) -> dict[str, Any]:
    with db.connection() as conn:
        product = catalog.detail(conn, product_id)
    if _should_log(request, agent):
        assert agent is not None
        with db.transaction() as conn:
            events.append(
                conn, trace_id=request.state.trace_id, type="product.read",
                actor_type="agent", actor_id=agent.agent_id, audience="aimo_only",
                text=f"Agent read product {product_id} ({product.name.get('en') or ''}).",
                body={"product_id": product_id, "result_count": 1},
            )
    return product.dump()
