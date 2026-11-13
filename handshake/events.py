"""Append-only, hash-chained event log (document 05, section 7; tasks T-206 and T-401).

Chains are per trace (A-20):

    canonical = JSON of {"event_id","trace_id","seq","type","at","actor_type","actor_id","body"}
                keys sorted, separators "," and ":", UTF-8, "at" as UTC ISO 8601 with ms
    hash      = SHA-256(prev_hash_hex + "\\n" + canonical), lowercase hex
    prev_hash = previous event's hash in the trace; 64 zeros for seq 1

Call `append()` inside the caller's transaction so the event commits with the change it records.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import Connection

from . import db, timeutil
from .ids import new_id

GENESIS = "0" * 64
ActorType = Literal["agent", "approver", "gateway", "admin"]
Audience = Literal["both", "aimo_only"]


def normalise_body(body: dict[str, Any] | None) -> dict[str, Any]:
    """Round-trip through JSON so the hashed body equals what the database returns."""
    return json.loads(json.dumps(body or {}, default=str, ensure_ascii=False))


def canonical(
    *, event_id: str, trace_id: str, seq: int, type: str, at: datetime,
    actor_type: str, actor_id: str | None, body: dict[str, Any],
) -> bytes:
    doc = {
        "event_id": event_id, "trace_id": trace_id, "seq": seq, "type": type,
        "at": timeutil.iso_utc_ms(at), "actor_type": actor_type, "actor_id": actor_id,
        "body": body,
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def chain_hash(prev_hash: str, canonical_bytes: bytes) -> str:
    return hashlib.sha256(prev_hash.encode() + b"\n" + canonical_bytes).hexdigest()


def append(
    conn: Connection, *, trace_id: str, type: str, actor_type: ActorType,
    text: str, body: dict[str, Any] | None = None, actor_id: str | None = None,
    audience: Audience = "both", at: datetime | None = None,
) -> dict[str, Any]:
    """Append one event to `trace_id`'s chain. Returns event_id, seq, hash."""
    at = at or timeutil.now()
    body = normalise_body(body)
    last = db.q(
        conn,
        "SELECT seq, hash FROM events WHERE trace_id = :t ORDER BY seq DESC LIMIT 1 FOR UPDATE",
        t=trace_id,
    ).first()
    seq, prev = (last[0] + 1, last[1]) if last else (1, GENESIS)
    event_id = new_id("evt")
    h = chain_hash(prev, canonical(event_id=event_id, trace_id=trace_id, seq=seq, type=type,
                                   at=at, actor_type=actor_type, actor_id=actor_id, body=body))
    db.q(
        conn,
        "INSERT INTO events (event_id, trace_id, seq, type, at, actor_type, actor_id, body_json,"
        " text, audience, prev_hash, hash) VALUES (:e, :t, :s, :ty, :at, :at_, :aid, :b, :tx,"
        " :au, :p, :h)",
        e=event_id, t=trace_id, s=seq, ty=type, at=timeutil.to_db(at), at_=actor_type,
        aid=actor_id, b=db.jdump(body), tx=text[:500], au=audience, p=prev, h=h,
    )
    return {"event_id": event_id, "seq": seq, "hash": h}


def list_events(
    conn: Connection, trace_id: str, audience: Audience | None = None
) -> list[dict[str, Any]]:
    """Events of one trace in order. `audience='both'` gives the buyer view."""
    sql = "SELECT * FROM events WHERE trace_id = :t"
    if audience == "both":
        sql += " AND audience = 'both'"
    rows = db.q(conn, sql + " ORDER BY seq", t=trace_id).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["at"] = timeutil.from_db(d["at"])
        d["body"] = db.jload(d.pop("body_json"))
        out.append(d)
    return out


def recent(conn: Connection, limit: int = 100, trace_id: str | None = None) -> list[dict]:
    if trace_id:
        return list_events(conn, trace_id)[-limit:]
    rows = db.q(conn, "SELECT * FROM events ORDER BY at DESC, seq DESC LIMIT :n",
                n=limit).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["at"] = timeutil.from_db(d["at"])
        d["body"] = db.jload(d.pop("body_json"))
        out.append(d)
    return out


def verify_chain(events: list[dict[str, Any]]) -> tuple[bool, int | None]:
    """Recompute each hash in order. Returns (ok, first bad seq or None)."""
    prev = GENESIS
    for i, e in enumerate(events, start=1):
        if e["seq"] != i or e["prev_hash"] != prev:
            return False, e["seq"]
        h = chain_hash(prev, canonical(
            event_id=e["event_id"], trace_id=e["trace_id"], seq=e["seq"], type=e["type"],
            at=e["at"], actor_type=e["actor_type"], actor_id=e["actor_id"],
            body=normalise_body(e["body"])))
        if h != e["hash"]:
            return False, e["seq"]
        prev = h
    return True, None
