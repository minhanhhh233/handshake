"""Valio Aimo operations role of the public demo, scoped to the visitor's sandbox (stage 4;
document 09, section 6g).

What Valio Aimo sees about the sandbox's agents: which agent read which products and prices
(search, product reads, upstream price and stock queries), every offer and order with the full
Valio Aimo receipt view, a suspend switch per agent, and a "tamper with the log" control that
alters one event of a sandbox order so the receipt reports the record as broken. Nothing here
reaches outside the caller's own sandbox.
"""

from __future__ import annotations

import json
from typing import Any

from . import db, timeutil
from . import sandbox as sbx

ACCESS_TYPES = ("search.performed", "product.read", "upstream.queried", "offer.requested")
#: The selling agent's seasonal offer per category: product, sales units, reason line.
SEASONAL = {
    "22300": ("467004", 2, "Seasonal offer: Kulta Katriina filter coffee for your winter guests."),
    "22410": ("464146", 2, "Seasonal offer: organic Fairtrade coffee beans for the holidays."),
    "10400": ("464306", 24, "Seasonal offer: lactose-free milk at this week's price."),
    "10900": ("472236", 24, "Seasonal offer: Finnish whole grain rye bread."),
    "10901": ("466005", 12, "Seasonal offer: toast bread for breakfast service."),
    "21380": ("465236", 4, "Seasonal offer: Rummo spaghetti for your pasta night."),
}
DEFAULT_SEASONAL = SEASONAL["22300"]


class OpsError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _agents(conn: Any, sandbox_id: str) -> list[dict[str, Any]]:
    rows = db.q(conn, "SELECT a.agent_id, a.name, a.status, c.name AS customer, "
                      "c.sandbox_persona AS persona FROM agents a JOIN customers c ON "
                      "c.customer_id = a.customer_id WHERE c.sandbox_id = :s ORDER BY a.created_at",
                s=sandbox_id).mappings().all()
    return [dict(r) for r in rows]


def view(sandbox_id: str) -> dict[str, Any]:
    with db.connection() as conn:
        agents = _agents(conn, sandbox_id)
        ids = [a["agent_id"] for a in agents]
        names = {a["agent_id"]: a["name"] for a in agents}
        access: list[dict[str, Any]] = []
        offers: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        if ids:
            ph = ", ".join(f":a{i}" for i in range(len(ids)))
            params = {f"a{i}": v for i, v in enumerate(ids)}
            tp = ", ".join(f"'{t}'" for t in ACCESS_TYPES)
            # upstream.queried has the gateway as actor and the agent in its body.
            for r in db.q(conn, f"SELECT at, type, text, actor_id, body_json FROM events WHERE "
                                f"type IN ({tp}) AND (actor_id IN ({ph}) OR JSON_UNQUOTE("
                                f"JSON_EXTRACT(body_json, '$.agent_id')) IN ({ph})) "
                                f"ORDER BY at DESC, seq DESC LIMIT 40", **params).mappings().all():
                body = db.jload(r["body_json"]) or {}
                who = r["actor_id"] if r["actor_id"] in names else body.get("agent_id")
                access.append({
                    "at": timeutil.iso(timeutil.from_db(r["at"])), "type": r["type"],
                    "agent": names.get(who, "?"), "text": r["text"],
                    "products": (body.get("product_ids") or [])[:12],
                })
            for r in db.q(conn, f"SELECT offer_id, agent_id, status, issued_at, "
                                f"JSON_UNQUOTE(JSON_EXTRACT(payload_json, '$.totals.excl_tax')) "
                                f"AS total FROM offers WHERE agent_id IN ({ph}) ORDER BY "
                                f"issued_at DESC LIMIT 30", **params).mappings().all():
                offers.append({"offer_id": r["offer_id"], "agent": names.get(r["agent_id"]),
                               "status": r["status"], "total_excl_tax": r["total"],
                               "issued_at": timeutil.iso(timeutil.from_db(r["issued_at"]))})
            for r in db.q(conn, f"SELECT o.order_id, o.agent_id, o.status, o.created_at, "
                                f"JSON_UNQUOTE(JSON_EXTRACT(f.payload_json, '$.totals.excl_tax')) "
                                f"AS total FROM orders o JOIN offers f ON f.offer_id = o.offer_id "
                                f"WHERE o.agent_id IN ({ph}) ORDER BY o.created_at DESC LIMIT 30",
                          **params).mappings().all():
                orders.append({"order_id": r["order_id"], "agent": names.get(r["agent_id"]),
                               "status": r["status"], "total_excl_tax": r["total"],
                               "created_at": timeutil.iso(timeutil.from_db(r["created_at"])),
                               "receipt_path": f"/demo/ops/receipts/{r['order_id']}"})
        props: list[dict[str, Any]] = []
        if ids:
            for r in db.q(conn, f"SELECT proposal_id, agent_id, product_id, status, "
                                f"reason_untrusted, blocked_reason, created_at FROM proposals "
                                f"WHERE agent_id IN ({ph}) ORDER BY created_at DESC LIMIT 30",
                          **params).mappings().all():
                props.append({"proposal_id": r["proposal_id"], "agent": names.get(r["agent_id"]),
                              "product_id": r["product_id"], "status": r["status"],
                              "reason_untrusted": r["reason_untrusted"],
                              "blocked_reason": r["blocked_reason"],
                              "created_at": timeutil.iso(timeutil.from_db(r["created_at"]))})
    return {"proposals": props,
            "agents": [{"agent_id": a["agent_id"], "name": a["name"], "status": a["status"],
                        "customer": a["customer"], "persona": a["persona"],
                        "persona_label": sbx.PERSONAS.get(a["persona"] or "", {}).get("label")}
                       for a in agents],
            "access": access, "offers": offers, "orders": orders}


def _own_agent(conn: Any, sandbox_id: str, agent_id: Any) -> dict[str, Any]:
    if not isinstance(agent_id, str):
        raise OpsError(400, "invalid_request", "Choose an agent.")
    for a in _agents(conn, sandbox_id):
        if a["agent_id"] == agent_id:
            return a
    raise OpsError(404, "not_found", "No such agent in your sandbox.")


def set_suspended(sandbox_id: str, agent_id: Any, suspended: bool) -> dict[str, Any]:
    """Suspend or reinstate one of the sandbox's agents (writes an admin event)."""
    from . import events
    from .ids import new_id

    with db.transaction() as conn:
        a = _own_agent(conn, sandbox_id, agent_id)
        status = "suspended" if suspended else "active"
        db.q(conn, "UPDATE agents SET status = :st WHERE agent_id = :a", st=status,
             a=a["agent_id"])
        events.append(conn, trace_id=new_id("trc"), type="agent.status_changed",
                      actor_type="admin", actor_id="Valio Aimo operations (you)",
                      audience="aimo_only",
                      text=f"Valio Aimo operations {'suspended' if suspended else 'reinstated'} "
                           f"{a['name']} ({a['customer']}).",
                      body={"agent_id": a["agent_id"], "status": status})
    return view(sandbox_id)


def order_in_sandbox(conn: Any, sandbox_id: str, order_id: Any) -> dict[str, Any] | None:
    if not isinstance(order_id, str):
        return None
    return db.q(conn, "SELECT o.order_id, o.trace_id FROM orders o JOIN customers c ON "
                      "c.customer_id = o.customer_id WHERE o.order_id = :o AND "
                      "c.sandbox_id = :s", o=order_id, s=sandbox_id).mappings().first()


def tamper(sandbox_id: str, order_id: Any) -> dict[str, Any]:
    """Alter one event of a sandbox order's trace without updating its hash: the policy
    outcome is rewritten to 'accepted'. The receipt then reports the record as broken."""
    with db.transaction() as conn:
        o = order_in_sandbox(conn, sandbox_id, order_id)
        if o is None:
            raise OpsError(404, "not_found", "No such order in your sandbox.")
        ev = db.q(conn, "SELECT event_id, seq, type, body_json FROM events WHERE trace_id = :t "
                        "AND type = 'policy.evaluated' ORDER BY seq LIMIT 1",
                  t=o["trace_id"]).mappings().first()
        if ev is None:
            ev = db.q(conn, "SELECT event_id, seq, type, body_json FROM events WHERE "
                            "trace_id = :t ORDER BY seq LIMIT 1", t=o["trace_id"]).mappings().first()
        body = db.jload(ev["body_json"]) or {}
        before = body.get("outcome")
        body["outcome"] = "accepted"
        body["reasons"] = []
        body["demo_tampered"] = "altered by the Valio Aimo operations demo control"
        db.q(conn, "UPDATE events SET body_json = :b WHERE event_id = :e",
             b=json.dumps(body, ensure_ascii=False), e=ev["event_id"])
    return {"order_id": o["order_id"], "seq": ev["seq"], "type": ev["type"],
            "changed": {"outcome": {"from": before, "to": "accepted"}},
            "receipt_path": f"/demo/ops/receipts/{o['order_id']}"}


def propose(sandbox_id: str, agent_id: Any, product_id: Any = None,
            trace_id: str | None = None) -> dict[str, Any]:
    """"Send seasonal offer": the selling agent picks a product in a category the buyer opted in
    to (or the demo's chosen product) and asks the gateway to send it. The gateway decides."""
    from . import mandates, proposals

    with db.connection() as conn:
        a = _own_agent(conn, sandbox_id, agent_id)
        m = mandates.get_current(conn, a["agent_id"])
    cats = list(m.proactive_offers.categories) if m and m.proactive_offers.enabled else []
    pick = SEASONAL.get(cats[0], DEFAULT_SEASONAL) if cats else DEFAULT_SEASONAL
    if isinstance(product_id, str) and product_id:
        pick = next((v for v in SEASONAL.values() if v[0] == product_id),
                    (product_id, 12, "Seasonal offer from Valio Aimo."))
    return proposals.send(a["agent_id"], pick[0], pick[1], pick[2], trace_id=trace_id)
