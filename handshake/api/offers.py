"""Offers (document 03, section 5; tasks T-106 and T-108)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import offers
from ..models import OfferRequest
from .deps import AgentContext, require_agent

router = APIRouter(tags=["offers"])


@router.post("/v1/offers", status_code=201, summary="Request a signed offer")
async def create_offer(
    body: OfferRequest, request: Request, agent: AgentContext = Depends(require_agent)
) -> JSONResponse:
    """Builds, holds, signs and stores one offer (`best`). See document 03, section 5."""
    result = await run_in_threadpool(
        offers.build_offer, body, agent_id=agent.agent_id, customer_id=agent.customer_id,
        trace_id=request.state.trace_id,
    )
    return JSONResponse(result, status_code=201)


@router.get("/v1/offers/{offer_id}", summary="Read one of your offers")
async def get_offer(offer_id: str, agent: AgentContext = Depends(require_agent)) -> JSONResponse:
    """Returns `{offer, jws}` with the current status (lazy expiry applied)."""
    return JSONResponse(await run_in_threadpool(offers.get_offer_view, offer_id, agent.agent_id))
