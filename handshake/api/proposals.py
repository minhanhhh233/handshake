"""Proactive offers for the agent (stage 6; document 03, section 5b).

GET  /v1/proposals                       the agent's open proposals (each a signed offer)
POST /v1/proposals/{proposal_id}/decline decline one; its stock hold is released
POST /v1/admin/proposals                 Valio Aimo's selling side sends one (admin key)
Accepting a proposal is POST /v1/orders with its offer_id: every policy rule applies.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .. import proposals
from ..ids import is_id
from ..errors import HandshakeError
from .deps import AgentContext, require_active_agent, require_admin

router = APIRouter(tags=["proposals"])


@router.get("/v1/proposals", summary="Open proactive offers for this agent")
def list_proposals(agent: AgentContext = Depends(require_active_agent)) -> dict:  # noqa: B008
    return proposals.list_open(agent.agent_id)


@router.post("/v1/proposals/{proposal_id}/decline", summary="Decline a proactive offer")
def decline_proposal(proposal_id: str,
                     agent: AgentContext = Depends(require_active_agent)) -> dict:  # noqa: B008
    if not is_id(proposal_id, "prp"):
        raise HandshakeError("not_found", f"Proposal {proposal_id} does not exist.")
    return proposals.decline(agent.agent_id, proposal_id)


class SendProposal(BaseModel):
    agent_id: str
    product_id: str = Field(min_length=1, max_length=16)
    quantity: int = Field(ge=1, le=1000)
    reason: str = Field(default="", max_length=500)


@router.post("/v1/admin/proposals", summary="Send a proactive offer (Valio Aimo, admin key)",
             dependencies=[Depends(require_admin)])
def send_proposal(body: SendProposal) -> dict:
    return proposals.send(body.agent_id, body.product_id, body.quantity, body.reason)
