"""Authentication dependencies (task T-103; document 02, section 6).

- Agents: `Authorization: Bearer <agent_key>`; keys are stored as SHA-256 hashes.
- Admin: `X-Admin-Key` header (pages may use the `key` query parameter).
- Anonymous: no credential; product search and discovery only (FR-05).
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from fastapi import Header, Query, Request

from .. import db
from ..config import get_settings
from ..errors import HandshakeError, forbidden, unauthenticated


@dataclass(frozen=True)
class AgentContext:
    agent_id: str
    customer_id: str
    customer_name: str
    name: str
    status: str  # active | suspended


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def lookup_agent(key: str) -> AgentContext | None:
    with db.connection() as conn:
        row = db.q(
            conn,
            "SELECT a.agent_id, a.customer_id, c.name AS customer_name, a.name, a.status "
            "FROM agents a JOIN customers c ON c.customer_id = a.customer_id "
            "WHERE a.key_sha256 = :h",
            h=key_hash(key),
        ).mappings().first()
    return AgentContext(**row) if row else None


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise unauthenticated("Authorization header must be 'Bearer <agent_key>'.")
    return token.strip()


def optional_agent(
    request: Request, authorization: str | None = Header(default=None)
) -> AgentContext | None:
    """Anonymous callers get None. A supplied but unknown key is still a 401."""
    token = _bearer(authorization)
    if token is None:
        return None
    agent = lookup_agent(token)
    if agent is None:
        raise unauthenticated("Unknown agent key.")
    request.state.agent = agent
    return agent


def require_agent(
    request: Request, authorization: str | None = Header(default=None)
) -> AgentContext:
    agent = optional_agent(request, authorization)
    if agent is None:
        raise unauthenticated()
    return agent


def _admin_ok(supplied: str | None) -> bool:
    expected = get_settings().admin_key
    return bool(expected and supplied) and hmac.compare_digest(supplied, expected)


def require_admin(x_admin_key: str | None = Header(default=None)) -> None:
    if not _admin_ok(x_admin_key):
        raise forbidden("Admin key missing or wrong.")


def require_admin_page(key: str | None = Query(default=None)) -> str:
    """Human pages pass the admin key as `?key=`. Returns it for building links."""
    if not _admin_ok(key):
        raise forbidden("Admin key missing or wrong.")
    return key  # type: ignore[return-value]


def ensure_owner(agent: AgentContext, owner_agent_id: str) -> None:
    if agent.agent_id != owner_agent_id:
        raise forbidden()


__all__ = [
    "AgentContext", "HandshakeError", "ensure_owner", "key_hash", "lookup_agent",
    "optional_agent", "require_admin", "require_admin_page", "require_agent",
]
