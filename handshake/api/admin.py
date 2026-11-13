"""Admin endpoints (document 03, section 7; task T-202).

All routes require `X-Admin-Key` (router-level dependency). Phase 2 implements mandates and
events; the approval-link and reset endpoints are added in phase 3.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from pydantic import ValidationError

from .. import db, events, mandates, timeutil
from ..errors import forbidden, invalid_request, not_found
from ..models import MandateBody
from .deps import require_admin

router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin)])


# ---------------------------------------------------------------- mandates


def _agent_customer(conn, agent_id: str) -> str:  # noqa: ANN001
    row = db.q(conn, "SELECT customer_id FROM agents WHERE agent_id = :a", a=agent_id).first()
    if row is None:
        raise not_found(f"Unknown agent {agent_id}.")
    return row[0]


def _parse_mandate(raw: dict[str, Any]) -> MandateBody:
    """Validate explicitly: a malformed money string raises decimal.InvalidOperation inside
    the shared validator, which Pydantic does not convert to a ValidationError."""
    try:
        return MandateBody.model_validate(raw)
    except ValidationError as e:
        errors = [{"field": ".".join(str(p) for p in err.get("loc", ())),
                   "message": err.get("msg", "invalid")} for err in e.errors()]
        raise invalid_request("Mandate document failed validation.", errors) from None
    except (ArithmeticError, ValueError) as e:
        raise invalid_request("Mandate document failed validation: a money value is not a "
                              "decimal string.", [{"field": "", "message": str(e)}]) from None


@router.put("/v1/admin/agents/{agent_id}/mandate", summary="Replace an agent's mandate")
def put_mandate(
    agent_id: str,
    raw: dict[str, Any] = Body(..., description="Mandate document (document 05, section 2) "
                               "without mandate_id, version, agent_id and customer_id."),
) -> dict[str, Any]:
    """Stores the body as the agent's mandate. Keeps `mandate_id`; `version` goes up by one."""
    body = _parse_mandate(raw)
    with db.transaction() as conn:
        customer_id = _agent_customer(conn, agent_id)
        mandate = mandates.replace(conn, agent_id, customer_id, body)
    return mandate.dump()


@router.get("/v1/admin/agents/{agent_id}/mandate", summary="Read an agent's full mandate")
def get_mandate(agent_id: str) -> dict[str, Any]:
    """Full mandate including approver contacts."""
    with db.connection() as conn:
        _agent_customer(conn, agent_id)
        mandate = mandates.get_current(conn, agent_id)
    if mandate is None:
        raise not_found(f"Agent {agent_id} has no mandate.")
    return mandate.dump()


# ---------------------------------------------------------------- events


def event_view(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": e["event_id"],
        "trace_id": e["trace_id"],
        "seq": e["seq"],
        "type": e["type"],
        "at": timeutil.iso(e["at"]),
        "actor": {"type": e["actor_type"], "id": e["actor_id"]},
        "audience": e["audience"],
        "text": e["text"],
        "body": e["body"],
        "hash": e["hash"],
    }


@router.get("/v1/admin/events", summary="Read events")
def list_events(
    trace_id: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """With `trace_id`: that trace's events in `seq` order (the last `limit`).
    Without: the most recent events across all traces, newest first."""
    with db.connection() as conn:
        rows = events.recent(conn, limit=limit, trace_id=trace_id or None)
    return {"events": [event_view(e) for e in rows]}


# ---------------------------------------------------------------- phase 3 (T-303)


@router.get("/v1/admin/orders/{order_id}/approval-link", summary="Approval link of an order")
def approval_link(order_id: str) -> dict[str, Any]:
    """The human approval URL (built from PUBLIC_BASE_URL). Out-of-band delivery only: no
    agent endpoint ever returns it (FR-35). Applies a due approval expiry first."""
    from .. import orders

    with db.transaction() as conn:
        order = orders.load_order(conn, order_id, for_update=True)
        approval = orders.load_approval(conn, order_id) if order else None
        if order is None or approval is None:
            raise not_found(f"Order {order_id} does not exist or needs no approval.")
        orders.expire_approval_if_due(conn, order_id)
        approval = orders.load_approval(conn, order_id)
    assert approval is not None
    return {
        "order_id": order_id,
        "approval_url": orders.approval_url(approval["approval_id"]),
        "status": approval["status"],
        "expires_at": timeutil.iso(approval["expires_at"]),
    }


@router.post("/v1/admin/reset", summary="Reset demo trading data (DEMO_MODE only)")
def reset() -> dict[str, Any]:
    """Deletes offers, holds, orders, approvals, idempotency keys and events; keeps the
    catalog, customers, agents and mandates. Refused unless DEMO_MODE=true."""
    from .. import config

    if not config.get_settings().demo_mode:
        raise forbidden("Reset is available only when DEMO_MODE is true.")
    from .. import demo

    with db.transaction() as conn:
        return demo.reset(conn)
