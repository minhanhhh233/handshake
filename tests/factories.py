"""Test factories for customers, agents and mandates."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from handshake import db, mandates, timeutil
from handshake.api.deps import key_hash
from handshake.ids import new_id
from handshake.models import Mandate, MandateBody


@dataclass
class Party:
    customer_id: str
    agent_id: str
    key: str
    mandate: Mandate

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}


def mandate_body(**overrides: Any) -> MandateBody:
    """Base mandate from document 09 (Nordlys): cap 1000.00, all categories, no forbidden
    allergens, official_rules_only, weekdays 1 to 6."""
    doc: dict[str, Any] = {
        "valid_from": "2026-11-01T00:00:00+02:00",
        "valid_to": None,
        "approvers": [{"name": "Maria Virtanen", "contact": "maria@example.com"}],
        "limits": {"currency": "EUR", "max_order_total_excl_tax": "1000.00",
                   "max_weekly_total_excl_tax": None},
        "allowed_categories": None,
        "forbidden_allergens": {"codes": [], "states": ["CONTAINS", "MAY_CONTAIN"],
                                "unknown": "reject"},
        "substitution": {"auto_approve": "official_rules_only"},
        "delivery": {"allowed_weekdays": [1, 2, 3, 4, 5, 6]},
        "stock": {"require_confirmed": False},
    }
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(doc.get(k), dict):
            doc[k] = {**doc[k], **v}
        else:
            doc[k] = v
    return MandateBody.model_validate(doc)


def make_party(
    name: str = "Nordlys Hotels", status: str = "active", **mandate_overrides: Any
) -> Party:
    key = "hsk_" + secrets.token_urlsafe(24)
    customer_id, agent_id = new_id("cus"), new_id("agt")
    now = timeutil.to_db(timeutil.now())
    with db.transaction() as conn:
        db.q(conn, "INSERT INTO customers (customer_id, name, created_at) VALUES (:c, :n, :t)",
             c=customer_id, n=name, t=now)
        db.q(conn, "INSERT INTO agents (agent_id, customer_id, name, key_sha256, status, "
                   "created_at) VALUES (:a, :c, :n, :h, :s, :t)",
             a=agent_id, c=customer_id, n=f"{name} purchasing agent", h=key_hash(key),
             s=status, t=now)
        m = mandates.replace(conn, agent_id, customer_id, mandate_body(**mandate_overrides))
    return Party(customer_id, agent_id, key, m)
