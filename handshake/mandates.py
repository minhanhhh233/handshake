"""Mandate storage (document 05, section 2; document 04 `mandates`, system-versioned).

One row per agent. Replacing a mandate keeps its `mandate_id` and increments `version`;
earlier versions remain queryable with `FOR SYSTEM_TIME AS OF`.
"""

from __future__ import annotations

from sqlalchemy import Connection

from . import db, timeutil
from .ids import new_id
from .models import Mandate, MandateBody


def get_current(conn: Connection, agent_id: str, *, for_update: bool = False) -> Mandate | None:
    sql = "SELECT document FROM mandates WHERE agent_id = :a" + (" FOR UPDATE" if for_update else "")
    row = db.q(conn, sql, a=agent_id).first()
    return Mandate.model_validate(db.jload(row[0])) if row else None


def replace(conn: Connection, agent_id: str, customer_id: str, body: MandateBody) -> Mandate:
    """Store `body` as the agent's mandate; assigns mandate_id (first time) and next version."""
    now = timeutil.now()
    current = get_current(conn, agent_id, for_update=True)
    mandate = Mandate(
        **body.model_dump(),
        mandate_id=current.mandate_id if current else new_id("mnd"),
        version=current.version + 1 if current else 1,
        agent_id=agent_id,
        customer_id=customer_id,
    )
    params = dict(
        m=mandate.mandate_id, a=agent_id, v=mandate.version,
        vf=timeutil.to_db(mandate.valid_from),
        vt=timeutil.to_db(mandate.valid_to) if mandate.valid_to else None,
        d=db.jdump(mandate.dump()), u=timeutil.to_db(now),
    )
    if current:
        db.q(conn, "UPDATE mandates SET version = :v, valid_from = :vf, valid_to = :vt, "
                   "document = :d, updated_at = :u WHERE agent_id = :a", **params)
    else:
        db.q(conn, "INSERT INTO mandates (mandate_id, agent_id, version, valid_from, valid_to, "
                   "document, updated_at) VALUES (:m, :a, :v, :vf, :vt, :d, :u)", **params)
    return mandate
