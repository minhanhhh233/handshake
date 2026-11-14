"""Agent mandate (document 03, section 4; task T-202)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import db, mandates
from ..errors import not_found
from .deps import AgentContext, require_active_agent, require_agent

router = APIRouter(tags=["mandates"])


@router.get("/v1/mandates/current", summary="Read your current mandate")
def get_current_mandate(agent: AgentContext = Depends(require_active_agent)) -> dict:
    """The caller's mandate (document 05, section 2). Approver contacts are omitted."""
    with db.connection() as conn:
        mandate = mandates.get_current(conn, agent.agent_id)
    if mandate is None:
        raise not_found("No mandate is set for this agent. Ask your operator to set one.")
    return mandate.public()
