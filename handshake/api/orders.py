"""Orders, dry-run and receipts (document 03, section 6; tasks T-203, T-204, T-205)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import orders
from ..models import DryRunRequest, OrderRequest
from .deps import AgentContext, require_agent

router = APIRouter(tags=["orders"])


@router.post("/v1/orders/dry-run", summary="Evaluate an offer against the mandate")
async def dry_run(
    body: DryRunRequest, request: Request, agent: AgentContext = Depends(require_agent)
) -> JSONResponse:
    """Runs the policy evaluation without ordering. The offer stays active."""
    result, trace = await run_in_threadpool(
        orders.dry_run, body.offer_id, agent.agent_id, agent.status)
    request.state.trace_id = trace
    return JSONResponse(result, status_code=200)


@router.post(
    "/v1/orders", summary="Place an order from an offer",
    responses={201: {"description": "confirmed"}, 202: {"description": "pending_approval"},
               200: {"description": "rejected"}},
)
async def create_order(
    body: OrderRequest, request: Request, agent: AgentContext = Depends(require_agent),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    """Consumes the offer. 201 confirmed, 202 pending_approval, 200 rejected.

    Requires `Idempotency-Key` (8 to 64 characters). The response never contains the
    approval link or token (FR-35).
    """
    status, result = await run_in_threadpool(
        orders.place_order, body, agent_id=agent.agent_id, customer_id=agent.customer_id,
        agent_status=agent.status, idem_key=idempotency_key,
    )
    request.state.trace_id = result["trace_id"]
    return JSONResponse(result, status_code=status)


@router.get("/v1/orders/{order_id}", summary="Read one of your orders")
async def get_order(
    order_id: str, request: Request, agent: AgentContext = Depends(require_agent)
) -> JSONResponse:
    """Current status: pending_approval, confirmed, declined, expired or rejected."""
    result = await run_in_threadpool(orders.get_order, order_id, agent.agent_id)
    request.state.trace_id = result["trace_id"]
    return JSONResponse(result)
