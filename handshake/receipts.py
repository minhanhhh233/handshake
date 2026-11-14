"""Receipts (tasks T-402, T-403; document 03, sections 6 and 8; document 05, section 7.4).

`build_receipt` is the signed buyer receipt: events with audience `both`, while the chain head
and length cover every event of the trace, including `aimo_only` ones (A-23). The buyer can
compare the signed head with what Valio Aimo shows, but cannot recompute it alone.

`receipt_view` is what the receipt page renders: the receipt plus the chain verification
result over all events and, for the Valio Aimo view, the internal events and the upstream
snapshot ids recorded in `upstream.queried` bodies.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from sqlalchemy import Connection

from . import db, events, offers, orders, signing, timeutil
from .config import get_settings
from .errors import forbidden, not_found
from .models import ChainInfo, Receipt, ReceiptEvent

ViewAudience = Literal["buyer", "aimo"]

SHORT_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
                "Dec"]


# ================================================================ summary


def _qty(v: Any) -> str:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return str(v)
    d = d.quantize(Decimal(1)) if d == d.to_integral_value() else d.normalize()
    return f"{d:,}"


def _short_date(v: Any) -> str:
    try:
        d = date.fromisoformat(str(v)[:10])
    except ValueError:
        return str(v)
    return f"{d.day} {SHORT_MONTHS[d.month - 1]} {d.year}"


def _line_phrase(ln: dict[str, Any]) -> str:
    base, q = ln.get("base_quantity") or {}, ln.get("quantity") or {}
    name = ln.get("name") or f"product {ln.get('product_id')}"
    if base.get("unit") in ("L", "KG") and base.get("value") is not None:
        amount = f"{_qty(base['value'])} {base['unit'].replace('KG', 'kg')}"
    else:
        n = q.get("value")
        unit = "unit" if str(n) == "1" else "units"
        amount = f"{_qty(n)} {unit if q.get('unit') == 'SALES_UNIT' else q.get('unit') or unit}"
    return f"{amount} of {name}"


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def summary(order: dict[str, Any], payload: dict[str, Any],
            approval: dict[str, Any] | None) -> str:
    """One plain-language paragraph: what was ordered, the total, and who decided."""
    what = _join([_line_phrase(ln) for ln in payload.get("lines") or []]) or "nothing"
    when = _short_date(payload.get("delivery_date"))
    total = (payload.get("totals") or {}).get("excl_tax")
    total_s = f"Total {total} EUR excl. tax."
    name = approval["approver_name"] if approval else None
    status = order["status"]
    if status == "confirmed":
        who = (f"Approved by {name}." if approval and approval["status"] == "approved"
               else "Within the mandate, so no approval was needed.")
        return f"Ordered {what} for delivery on {when}. {total_s} {who}"
    if status == "pending_approval":
        return (f"Order for {what} for delivery on {when} is waiting for approval from "
                f"{name}. {total_s}")
    if status == "declined":
        return f"Order for {what} for delivery on {when} was declined by {name}. {total_s}"
    if status == "expired":
        return (f"Order for {what} for delivery on {when} was not placed: {name} did not "
                f"decide in time. {total_s}")
    reasons = [r.get("message") for r in order.get("reasons") or [] if r.get("message")]
    why = f" {' '.join(reasons)}" if reasons else ""
    return (f"Order for {what} for delivery on {when} was rejected by the mandate.{why} "
            f"{total_s}")


# ================================================================ receipt (T-402)


def _actor(e: dict[str, Any]) -> dict[str, Any]:
    a: dict[str, Any] = {"type": e["actor_type"]}
    if e.get("actor_id"):
        a["id"] = e["actor_id"]
    return a


def _receipt_event(e: dict[str, Any]) -> ReceiptEvent:
    return ReceiptEvent(seq=e["seq"], event_id=e["event_id"], type=e["type"], at=e["at"],
                        actor=_actor(e), text=e["text"], hash=e["hash"])


def _load(conn: Connection, order_id: str) -> dict[str, Any]:
    order = orders.load_order(conn, order_id)
    if order is None:
        raise not_found(f"Order {order_id} does not exist.")
    offer = offers.load_offer(conn, order["offer_id"])
    assert offer is not None  # offers are never deleted
    all_events = events.list_events(conn, order["trace_id"])
    return {"order": order, "offer": offer, "approval": orders.load_approval(conn, order_id),
            "events": all_events}


def _receipt(data: dict[str, Any]) -> Receipt:
    order, offer, evs = data["order"], data["offer"], data["events"]
    return Receipt(
        order_id=order["order_id"], trace_id=order["trace_id"], status=order["status"],
        generated_at=timeutil.now(),
        summary=summary(order, offer["payload"], data["approval"]),
        events=[_receipt_event(e) for e in evs if e["audience"] == "both"],
        chain=ChainInfo(head=evs[-1]["hash"] if evs else events.GENESIS, length=len(evs)),
        offer_jws=offer["jws"],
    )


def build_receipt(conn: Connection, order_id: str) -> Receipt:
    """The buyer receipt (document 03, section 6). Raises not_found."""
    return _receipt(_load(conn, order_id))


def human_url(order: dict[str, Any]) -> str:
    return (f"{get_settings().public_base_url}/receipts/{order['order_id']}"
            f"?t={order['receipt_token']}")


def get_receipt(order_id: str, agent_id: str) -> dict[str, Any]:
    """GET /v1/orders/{order_id}/receipt: own orders only (404, then 403)."""
    with db.transaction() as conn:
        order = orders.load_order(conn, order_id, for_update=True)
        if order is None:
            raise not_found(f"Order {order_id} does not exist.")
        if order["agent_id"] != agent_id:
            raise forbidden("This order belongs to another agent.")
        # Lazy approval expiry, as on GET /v1/orders/{order_id}, so the status is current.
        orders.expire_approval_if_due(conn, order_id)
        receipt = build_receipt(conn, order_id).dump()
    return {"receipt": receipt, "jws": signing.sign(receipt, signing.TYP_RECEIPT),
            "human_url": human_url(order)}


# ================================================================ page data (T-403)


def fragment(h: str | None) -> str:
    """'3f2a1b9c...0d1e': first 8 and last 4 hex characters."""
    if not h:
        return ""
    return f"{h[:8]}…{h[-4:]}" if len(h) > 12 else h


def _snapshots(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Per product: the price quote and availability snapshot ids of an upstream.queried."""
    prices = body.get("price_quote_ids") or {}
    avail = body.get("availability_snapshot_ids") or {}
    cached = set(body.get("cache_hit") or [])
    out = []
    for pid in body.get("product_ids") or sorted(set(prices) | set(avail)):
        out.append({"product_id": pid, "price_quote_id": prices.get(pid),
                    "availability_snapshot_id": avail.get(pid), "cached": pid in cached})
    return out


def receipt_view(conn: Connection, order_id: str, audience: ViewAudience) -> dict[str, Any]:
    """Data for the receipt page. `audience` is 'buyer' (events with audience both) or
    'aimo' (all events, with upstream snapshot references). Raises not_found."""
    data = _load(conn, order_id)
    order, offer, approval, all_events = (data["order"], data["offer"], data["approval"],
                                          data["events"])
    receipt = _receipt(data)
    ok, bad_seq = events.verify_chain(all_events)
    who = db.q(conn, "SELECT c.name AS customer_name, g.name AS agent_name FROM agents g "
                     "JOIN customers c ON c.customer_id = g.customer_id WHERE g.agent_id = :a",
               a=order["agent_id"]).mappings().first()
    agent_name = who["agent_name"] if who else "Agent"
    shown = [e for e in all_events if audience == "aimo" or e["audience"] == "both"]

    first_day = all_events[0]["at"].astimezone(timeutil.HELSINKI).date() if all_events else None
    by_seq = {e["seq"]: e for e in all_events}
    items = []
    prev_seq = 0
    for e in shown:
        actor_type = e["actor_type"]
        if actor_type == "agent":
            actor = "Your agent" if audience == "buyer" else agent_name
        elif actor_type == "approver":
            actor = e.get("actor_id") or "Approver"
        elif actor_type == "admin":
            actor = "Valio Aimo admin"
        else:
            actor = "Handshake gateway"
        local = e["at"].astimezone(timeutil.HELSINKI)
        prev = by_seq.get(e["seq"] - 1)
        items.append({
            "seq": e["seq"], "type": e["type"], "text": e["text"], "actor": actor,
            "actor_type": actor_type, "at": e["at"],
            "other_day": first_day is not None and local.date() != first_day,
            "hash": e["hash"], "hash_short": fragment(e["hash"]),
            "prev_short": fragment(prev["hash"]) if prev else None,
            "aimo_only": e["audience"] == "aimo_only",
            "hidden_before": e["seq"] - prev_seq - 1,
            "snapshots": _snapshots(e["body"]) if (audience == "aimo"
                                                   and e["type"] == "upstream.queried")
            else [],
            "altered": bad_seq == e["seq"],
        })
        prev_seq = e["seq"]

    payload = offer["payload"]
    return {
        "audience": audience,
        "receipt": receipt,
        "order": order,
        "status": order["status"],
        "summary": receipt.summary,
        "chain_ok": ok, "bad_seq": bad_seq,
        "chain_head": receipt.chain.head, "chain_head_short": fragment(receipt.chain.head),
        "chain_length": receipt.chain.length,
        "signing_kid": get_settings().signing_kid,
        "events": items,
        "hidden_count": len(all_events) - len(shown),
        "hidden_after": len(all_events) - prev_seq if all_events else 0,
        "first_day": first_day,
        "order_id": order["order_id"], "offer_id": order["offer_id"],
        "trace_id": order["trace_id"], "offer_jws": offer["jws"],
        "customer_name": who["customer_name"] if who else "",
        "agent_name": agent_name,
        "reference": order.get("reference"), "note": order.get("note_untrusted"),
        "approver_name": approval["approver_name"] if approval else None,
        "comment": approval["comment_untrusted"] if approval else None,
        "totals": payload.get("totals") or {},
        "delivery_date": payload.get("delivery_date"),
        "mandate_version": order.get("mandate_version"),
    }


__all__ = ["build_receipt", "fragment", "get_receipt", "human_url", "receipt_view", "summary"]
