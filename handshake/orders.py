"""Orders: dry-run, order creation, order view (tasks T-203, T-204, T-205).

Documents: 03 section 6 (API), 02 sections 5.3 to 5.5 and 9 (flows, state machines, locking),
05 sections 3, 6 and 7.1 (policy, holds, events), 04 (orders, approvals, idempotency_keys).

Flow of `POST /v1/orders`:

1. Idempotency-Key lookup: same body -> stored response; other body -> 409.
2. Load the offer (404 / 403); lazy expiry in its own transaction; 409 if expired or consumed.
3. One transaction: lock the offer, re-check, evaluate, insert order (+ approval), consume the
   offer, move holds, store the idempotency record, write events.

Helpers for phase 3 (approval page, admin console): `load_order`, `order_view`,
`commit_holds`, `approval_token`, `approval_url`, `find_approval_by_token`,
`expire_approval_if_due`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection
from sqlalchemy.exc import IntegrityError

from . import db, events, mandates, offers, policy, timeutil
from .config import get_settings
from .errors import (
    HandshakeError,
    forbidden,
    idempotency_conflict,
    invalid_request,
    not_found,
    offer_consumed,
    offer_expired,
)
from .ids import new_id, new_token
from .models import (
    ApproverName,
    DryRunResult,
    Evaluation,
    Mandate,
    Offer,
    Order,
    OrderApproval,
    OrderLinks,
    OrderRequest,
    Reason,
    Requirements,
    Totals,
)

STATUS_BY_OUTCOME = {
    "accepted": "confirmed",
    "needs_approval": "pending_approval",
    "rejected": "rejected",
}
HTTP_BY_STATUS = {"confirmed": 201, "pending_approval": 202, "rejected": 200}
IDEM_MIN, IDEM_MAX = 8, 64

# ================================================================ approval tokens


def approval_token(approval_id: str) -> str:
    """Secret approval token, derivable only with ADMIN_KEY (never stored in clear).

    base64url without padding of HMAC-SHA256(key=ADMIN_KEY, msg=approval_id): 43 characters.
    Lets the admin console show the approval link again without keeping the token.
    """
    key = get_settings().admin_key
    if not key:
        raise RuntimeError("ADMIN_KEY is not set; approval tokens cannot be derived")
    mac = hmac.new(key.encode("utf-8"), approval_id.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def approval_url(approval_id: str) -> str:
    """Human approval link (out of band only; never returned by an agent endpoint, FR-35)."""
    return f"{get_settings().public_base_url}/approve/{approval_token(approval_id)}"


def _approval_row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for k in ("requested_at", "expires_at", "decided_at"):
        d[k] = timeutil.from_db(d[k])
    return d


def find_approval_by_token(conn: Connection, token: str, *, for_update: bool = False
                           ) -> dict[str, Any] | None:
    """Approval row for a token from an approval link, or None."""
    sql = "SELECT * FROM approvals WHERE token_sha256 = :h" + (" FOR UPDATE" if for_update else "")
    return _approval_row(db.q(conn, sql, h=token_sha256(token)).mappings().first())


def load_approval(conn: Connection, order_id: str, *, for_update: bool = False
                  ) -> dict[str, Any] | None:
    sql = "SELECT * FROM approvals WHERE order_id = :o" + (" FOR UPDATE" if for_update else "")
    return _approval_row(db.q(conn, sql, o=order_id).mappings().first())


# ================================================================ reading


def load_order(conn: Connection, order_id: str, *, for_update: bool = False
               ) -> dict[str, Any] | None:
    """Order row as a dict: aware created_at/updated_at, `reasons` parsed (list of dicts)."""
    sql = "SELECT * FROM orders WHERE order_id = :o" + (" FOR UPDATE" if for_update else "")
    row = db.q(conn, sql, o=order_id).mappings().first()
    if row is None:
        return None
    d = dict(row)
    d["created_at"] = timeutil.from_db(d["created_at"])
    d["updated_at"] = timeutil.from_db(d["updated_at"])
    d["reasons"] = db.jload(d.pop("reasons_json")) or []
    return d


def _links(order_id: str) -> OrderLinks:
    return OrderLinks(self_=f"/v1/orders/{order_id}", receipt=f"/v1/orders/{order_id}/receipt")


def _approval_view(a: dict[str, Any] | None) -> OrderApproval | None:
    if a is None:
        return None
    decided = a["status"] != "pending"
    return OrderApproval(
        status=a["status"], approver=ApproverName(name=a["approver_name"]),
        requested_at=a["requested_at"], expires_at=a["expires_at"],
        decided_at=a["decided_at"] if decided else None,
        comment=a["comment_untrusted"] if decided else None,
    )


def _build_order(order: dict[str, Any], payload: dict[str, Any],
                 approval: dict[str, Any] | None) -> Order:
    return Order(
        order_id=order["order_id"], status=order["status"], outcome=order["outcome"],
        reasons=[Reason.model_validate(r) for r in order["reasons"]],
        offer_id=order["offer_id"], trace_id=order["trace_id"],
        totals=Totals.model_validate(payload["totals"]),
        delivery_date=payload["delivery_date"],
        approval=_approval_view(approval) if order["outcome"] == "needs_approval" else None,
        created_at=order["created_at"], links=_links(order["order_id"]),
    )


def order_view(conn: Connection, order_id: str) -> Order:
    """The agent-facing Order object with current status (no approval URL or token)."""
    order = load_order(conn, order_id)
    if order is None:
        raise not_found(f"Order {order_id} does not exist.")
    offer = offers.load_offer(conn, order["offer_id"])
    assert offer is not None
    return _build_order(order, offer["payload"], load_approval(conn, order_id))


# ================================================================ holds


def _hold_list(rows: Any) -> list[dict[str, Any]]:
    return [{"line_no": r["line_no"], "product_id": r["product_id"],
             "quantity_sales_units": str(offers._num(Decimal(r["quantity_sales_units"])))}
            for r in rows]


def commit_holds(conn: Connection, offer_id: str, trace_id: str) -> list[dict[str, Any]]:
    """`held` -> `committed` with expires_at NULL; writes `holds.changed` (aimo_only)."""
    rows = db.q(conn, "SELECT hold_id, line_no, product_id, quantity_sales_units FROM "
                      "stock_holds WHERE offer_id = :o AND status = 'held' FOR UPDATE",
                o=offer_id).mappings().all()
    if not rows:
        return []
    db.q(conn, "UPDATE stock_holds SET status = 'committed', expires_at = NULL "
               "WHERE offer_id = :o AND status = 'held'", o=offer_id)
    holds = _hold_list(rows)
    events.append(
        conn, trace_id=trace_id, type="holds.changed", actor_type="gateway",
        audience="aimo_only",
        text=f"Committed {len(rows)} hold(s) of offer {offer_id} (order confirmed).",
        body={"offer_id": offer_id, "status": "committed", "holds": holds},
    )
    return holds


def extend_holds(conn: Connection, offer_id: str, trace_id: str, until: datetime
                 ) -> list[dict[str, Any]]:
    """Keep holds `held` but move expires_at to the approval deadline (document 05, 6)."""
    rows = db.q(conn, "SELECT hold_id, line_no, product_id, quantity_sales_units FROM "
                      "stock_holds WHERE offer_id = :o AND status = 'held' FOR UPDATE",
                o=offer_id).mappings().all()
    if not rows:
        return []
    db.q(conn, "UPDATE stock_holds SET expires_at = :e WHERE offer_id = :o AND status = 'held'",
         o=offer_id, e=timeutil.to_db(until))
    holds = _hold_list(rows)
    events.append(
        conn, trace_id=trace_id, type="holds.changed", actor_type="gateway",
        audience="aimo_only",
        text=(f"Kept {len(rows)} hold(s) of offer {offer_id} until "
              f"{timeutil.iso(until)} (awaiting approval)."),
        body={"offer_id": offer_id, "status": "held", "expires_at": timeutil.iso(until),
              "holds": holds},
    )
    return holds


# ================================================================ approval expiry (doc 05, 6)


def expire_approval_if_due(conn: Connection, order_id: str) -> bool:
    """Lazy approval expiry (A-12). Call inside a transaction.

    If the order is `pending_approval` and its pending approval has passed `expires_at`:
    approval -> expired, order -> expired, holds released; events `approval.expired`,
    `order.closed` (both) and `holds.changed` (aimo_only). Returns True if it expired.
    """
    order = load_order(conn, order_id, for_update=True)
    if order is None or order["status"] != "pending_approval":
        return False
    a = load_approval(conn, order_id, for_update=True)
    now = timeutil.now()
    if a is None or a["status"] != "pending" or a["expires_at"] > now:
        return False
    at = a["expires_at"]
    db.q(conn, "UPDATE approvals SET status = 'expired', decided_at = :d "
               "WHERE approval_id = :a AND status = 'pending'",
         a=a["approval_id"], d=timeutil.to_db(at))
    db.q(conn, "UPDATE orders SET status = 'expired', updated_at = :u WHERE order_id = :o",
         o=order_id, u=timeutil.to_db(now))
    trace = order["trace_id"]
    events.append(
        conn, trace_id=trace, type="approval.expired", actor_type="gateway", audience="both",
        text=(f"{a['approver_name']} did not decide by {timeutil.iso(at)}. "
              f"The approval request expired."),
        body={"order_id": order_id, "expires_at": timeutil.iso(at)},
    )
    events.append(
        conn, trace_id=trace, type="order.closed", actor_type="gateway", audience="both",
        text=f"Order {order_id} closed as expired: no approval decision in time.",
        body={"order_id": order_id, "status": "expired"},
    )
    offers.release_holds(conn, order["offer_id"], trace, "approval expired")
    return True


# ================================================================ evaluation


def _requirements(offer_row: dict[str, Any]) -> dict[int, Requirements]:
    lines = (offer_row.get("request") or {}).get("lines") or []
    return {i + 1: Requirements(**(line.get("requirements") or {}))
            for i, line in enumerate(lines)}


def week_total(conn: Connection, agent_id: str, now: datetime) -> Decimal:
    """R7 input: totals excl. tax of this agent's confirmed and pending-approval orders created
    in the current ISO week (Monday 00:00 to Monday 00:00, Europe/Helsinki)."""
    local = now.astimezone(timeutil.HELSINKI)
    start = (local - timedelta(days=local.isoweekday() - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    total = db.q(
        conn,
        "SELECT COALESCE(SUM(f.total_excl_tax), 0) FROM orders o JOIN offers f "
        "ON f.offer_id = o.offer_id WHERE o.agent_id = :a "
        "AND o.status IN ('confirmed', 'pending_approval') "
        "AND o.created_at >= :s AND o.created_at < :e",
        a=agent_id, s=timeutil.to_db(start), e=timeutil.to_db(end),
    ).scalar_one()
    return Decimal(total)


def _evaluate(conn: Connection, offer_row: dict[str, Any], agent_status: str,
              now: datetime) -> tuple[Offer, Mandate, Evaluation]:
    mandate = mandates.get_current(conn, offer_row["agent_id"])
    if mandate is None:
        raise forbidden("This agent has no mandate. An admin must set one before orders "
                        "can be placed.")
    from . import trust

    mandate = trust.effective(conn, mandate)  # trust ladder: the current step is the cap
    offer = Offer.model_validate(offer_row["payload"])
    catalog = offers.load_catalog(conn, [ln.product_id for ln in offer.lines])
    facts = {pid: cp.facts() for pid, cp in catalog.items()}
    ev = policy.evaluate(offer, mandate, agent_status, facts,
                         week_total(conn, offer_row["agent_id"], now), now,
                         _requirements(offer_row))
    return offer, mandate, ev


def _check_offer(offer_id: str, agent_id: str) -> dict[str, Any]:
    """404/403, then lazy expiry committed in its own transaction, then 409s."""
    with db.transaction() as conn:
        row = offers.load_offer(conn, offer_id, for_update=True)
        if row is None:
            raise not_found(f"Offer {offer_id} does not exist.")
        if row["agent_id"] != agent_id:
            raise forbidden("This offer was issued to another agent.")
        row = offers.expire_if_due(conn, row)
    _raise_if_unusable(row)
    return row


def _raise_if_unusable(row: dict[str, Any]) -> None:
    if row["status"] == "expired":
        raise offer_expired(row["offer_id"], timeutil.iso(row["expires_at"]))
    if row["status"] == "consumed":
        raise offer_consumed(row["offer_id"])


# ================================================================ dry-run (T-203)


def dry_run(offer_id: str, agent_id: str, agent_status: str) -> tuple[dict[str, Any], str]:
    """POST /v1/orders/dry-run. Returns (DryRunResult JSON, offer trace_id)."""
    row = _check_offer(offer_id, agent_id)
    now = timeutil.now()
    with db.transaction() as conn:
        _, mandate, ev = _evaluate(conn, row, agent_status, now)
        codes = [r.code for r in ev.reasons]
        events.append(
            conn, trace_id=row["trace_id"], type="order.dry_run", actor_type="agent",
            actor_id=agent_id, audience="both",
            text=(f"Agent checked offer {offer_id} without ordering: outcome {ev.outcome}"
                  + (f" ({', '.join(codes)})." if codes else ".")),
            body={"offer_id": offer_id, "outcome": ev.outcome, "reason_codes": codes,
                  "mandate_version": mandate.version},
        )
    result = DryRunResult(
        offer_id=offer_id, outcome=ev.outcome, reasons=ev.reasons,
        mandate_version=mandate.version, evaluated_at=now,
        approver=ApproverName(name=mandate.approvers[0].name),
    )
    return result.dump(), row["trace_id"]


# ================================================================ orders (T-204)


def validate_idempotency_key(key: str | None) -> str:
    if key is None or not (IDEM_MIN <= len(key) <= IDEM_MAX):
        raise invalid_request(
            "Idempotency-Key header is required: 8 to 64 characters.",
            [{"field": "Idempotency-Key", "message": "Required; 8 to 64 characters."}],
        )
    return key


def request_hash(req: OrderRequest) -> str:
    canonical = json.dumps(req.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stored(conn: Connection, agent_id: str, key: str, *, lock: bool = False
            ) -> dict[str, Any] | None:
    sql = ("SELECT request_sha256, response_status, response_json FROM idempotency_keys "
           "WHERE agent_id = :a AND idem_key = :k" + (" FOR UPDATE" if lock else ""))
    row = db.q(conn, sql, a=agent_id, k=key).mappings().first()
    if row is None:
        return None
    return {"sha": row["request_sha256"], "status": row["response_status"],
            "body": db.jload(row["response_json"])}


def _replay(stored: dict[str, Any], sha: str) -> tuple[int, dict[str, Any]]:
    if stored["sha"] != sha:
        raise idempotency_conflict()
    return stored["status"], stored["body"]


def _mandate_text(mandate: Any) -> str:
    """The mandate version in force, for the policy.evaluated event (and so the receipt)."""
    cap = mandate.limits.max_order_total_excl_tax
    week = mandate.limits.max_weekly_total_excl_tax
    limits = f"per-order cap {cap} EUR" if cap is not None else "no per-order cap"
    if week is not None:
        limits += f", weekly cap {week} EUR"
    return f"Evaluated against mandate version {mandate.version} ({limits})."


def _policy_text(ev: Evaluation, offer: Offer, approver: str) -> str:
    total = offer.totals.excl_tax
    if ev.outcome == "accepted":
        return f"Total {total} EUR excl. tax is within the mandate. No approval needed."
    parts = []
    for r in ev.reasons:
        d = r.details or {}
        if r.code == "spend_cap_exceeded" and d.get("cap"):
            parts.append(f"Total {d.get('total_excl_tax', total)} EUR exceeded the "
                         f"{d['cap']} EUR cap.")
        else:
            parts.append(r.message)
    if ev.outcome == "needs_approval":
        return " ".join(parts + [f"Sent to {approver} for approval."])
    return " ".join(["Rejected by the mandate."] + parts)


def place_order(req: OrderRequest, *, agent_id: str, customer_id: str, agent_status: str,
                idem_key: str | None, approval_ttl_seconds: int | None = None
                ) -> tuple[int, dict[str, Any]]:
    """POST /v1/orders. Returns (HTTP status, Order JSON). Raises HandshakeError.

    `approval_ttl_seconds` overrides APPROVAL_TTL_SECONDS (the public demo uses 30 minutes)."""
    key = validate_idempotency_key(idem_key)
    sha = request_hash(req)
    with db.connection() as conn:
        stored = _stored(conn, agent_id, key)
    if stored is not None:
        return _replay(stored, sha)

    try:
        _check_offer(req.offer_id, agent_id)
        return _create(req, agent_id, customer_id, agent_status, key, sha,
                       approval_ttl_seconds)
    except HandshakeError as e:
        # A same-key request that overlapped the original sees the offer as consumed. Return
        # the original's stored result instead of offer_consumed, so a retrying agent is not
        # led to order again (code review, Important #1).
        if e.code != "offer_consumed":
            raise
        with db.connection() as conn:
            stored = _stored(conn, agent_id, key)
        if stored is not None:
            return _replay(stored, sha)
        raise
    except IntegrityError:
        # A concurrent request won the race on the idempotency key or on orders.offer_id.
        with db.connection() as conn:
            stored = _stored(conn, agent_id, key)
        if stored is not None:
            return _replay(stored, sha)
        raise offer_consumed(req.offer_id) from None


def _create(req: OrderRequest, agent_id: str, customer_id: str, agent_status: str, key: str,
            sha: str, approval_ttl_seconds: int | None = None) -> tuple[int, dict[str, Any]]:
    error: HandshakeError | None = None
    result: tuple[int, dict[str, Any]] | None = None
    with db.transaction() as conn:
        row = offers.load_offer(conn, req.offer_id, for_update=True)
        assert row is not None  # offers are never deleted
        stored = _stored(conn, agent_id, key)
        if stored is not None:
            result = _replay(stored, sha)
        else:
            row = offers.expire_if_due(conn, row)  # commits the expiry, then 409 below
            if row["status"] != "active":
                error = (offer_expired(row["offer_id"], timeutil.iso(row["expires_at"]))
                         if row["status"] == "expired" else offer_consumed(row["offer_id"]))
            else:
                result = _insert_order(conn, req, row, agent_id, customer_id, agent_status,
                                       key, sha, approval_ttl_seconds)
    if error is not None:
        raise error
    assert result is not None
    return result


def _insert_order(conn: Connection, req: OrderRequest, row: dict[str, Any], agent_id: str,
                  customer_id: str, agent_status: str, key: str, sha: str,
                  approval_ttl_seconds: int | None = None) -> tuple[int, dict[str, Any]]:
    now = timeutil.now()
    offer, mandate, ev = _evaluate(conn, row, agent_status, now)
    trace, offer_id = row["trace_id"], row["offer_id"]
    order_id = new_id("ord")
    status = STATUS_BY_OUTCOME[ev.outcome]
    approver = mandate.approvers[0]
    reasons = [r.dump() for r in ev.reasons]

    db.q(conn,
         "INSERT INTO orders (order_id, offer_id, trace_id, agent_id, customer_id, mandate_id, "
         "mandate_version, status, outcome, reasons_json, reference, note_untrusted, "
         "rationale_untrusted, "
         "receipt_token, created_at, updated_at) VALUES (:o, :of, :t, :a, :c, :m, :mv, :s, "
         ":oc, :r, :ref, :note, :rat, :rt, :ca, :ca)",
         o=order_id, of=offer_id, t=trace, a=agent_id, c=customer_id,
         m=mandate.mandate_id, mv=mandate.version, s=status, oc=ev.outcome,
         r=db.jdump(reasons), ref=req.reference, note=req.note, rat=req.rationale,
         rt=new_token(),
         ca=timeutil.to_db(now))
    db.q(conn, "UPDATE offers SET status = 'consumed' WHERE offer_id = :o", o=offer_id)

    approval: dict[str, Any] | None = None
    if ev.outcome == "needs_approval":
        expires = now + timedelta(
            seconds=approval_ttl_seconds or get_settings().approval_ttl_seconds)
        for ln in offer.lines:
            ob = ln.availability.order_by
            if ob is not None and ob < expires:
                expires = ob
        approval_id = new_id("apr")
        db.q(conn,
             "INSERT INTO approvals (approval_id, order_id, token_sha256, approver_name, "
             "approver_contact, status, requested_at, expires_at) VALUES (:id, :o, :h, :n, "
             ":ct, 'pending', :ra, :ea)",
             id=approval_id, o=order_id, h=token_sha256(approval_token(approval_id)),
             n=approver.name, ct=approver.contact, ra=timeutil.to_db(now),
             ea=timeutil.to_db(expires))
        approval = {"approval_id": approval_id, "approver_name": approver.name,
                    "status": "pending", "requested_at": now, "expires_at": expires,
                    "decided_at": None, "comment_untrusted": None}

    order = Order(
        order_id=order_id, status=status, outcome=ev.outcome, reasons=ev.reasons,
        offer_id=offer_id, trace_id=trace, totals=offer.totals,
        delivery_date=offer.delivery_date, approval=_approval_view(approval),
        created_at=now, links=_links(order_id),
    )
    body = order.dump()
    http = HTTP_BY_STATUS[status]
    db.q(conn,
         "INSERT INTO idempotency_keys (agent_id, idem_key, request_sha256, response_status, "
         "response_json, created_at) VALUES (:a, :k, :h, :s, :b, :ca)",
         a=agent_id, k=key, h=sha, s=http, b=db.jdump(body), ca=timeutil.to_db(now))

    # Events (document 05, 7.1), in order.
    total = offer.totals.excl_tax
    ref = f" (reference {req.reference})" if req.reference else ""
    events.append(
        conn, trace_id=trace, type="order.submitted", actor_type="agent", actor_id=agent_id,
        audience="both", at=now,
        text=f"Agent submitted an order for offer {offer_id}{ref}, total {total} EUR excl. tax.",
        body={"order_id": order_id, "offer_id": offer_id, "reference": req.reference,
              **({"rationale_untrusted": req.rationale} if req.rationale else {})},
    )
    events.append(
        conn, trace_id=trace, type="policy.evaluated", actor_type="gateway", audience="both",
        text=(_policy_text(ev, offer, approver.name) + " " + _mandate_text(mandate)),
        body={"order_id": order_id, "outcome": ev.outcome, "reasons": reasons,
              "mandate_id": mandate.mandate_id, "mandate_version": mandate.version},
    )
    if ev.outcome == "needs_approval":
        assert approval is not None
        until = approval["expires_at"]
        events.append(
            conn, trace_id=trace, type="approval.requested", actor_type="gateway",
            audience="both",
            text=(f"Approval requested from {approver.name}; the request expires at "
                  f"{until.astimezone(timeutil.HELSINKI).strftime('%H:%M')}."),
            body={"order_id": order_id, "approver": {"name": approver.name},
                  "expires_at": timeutil.iso(until)},
        )
        extend_holds(conn, offer_id, trace, until)
    elif ev.outcome == "accepted":
        events.append(
            conn, trace_id=trace, type="order.confirmed", actor_type="gateway", audience="both",
            text=(f"Order {order_id} confirmed: {total} EUR excl. tax, delivery "
                  f"{offer.delivery_date.isoformat()}."),
            body={"order_id": order_id, "status": "confirmed", "totals": offer.totals.dump()},
        )
        commit_holds(conn, offer_id, trace)
    else:
        events.append(
            conn, trace_id=trace, type="order.closed", actor_type="gateway", audience="both",
            text=f"Order {order_id} closed as rejected; its holds are released.",
            body={"order_id": order_id, "status": "rejected",
                  "reason_codes": [r.code for r in ev.reasons]},
        )
        offers.release_holds(conn, offer_id, trace, "order rejected")
    if ev.outcome in ("accepted", "rejected"):
        from . import trust

        trust.note_change(conn, mandates.get_current(conn, agent_id), order_id, trace)
    return http, body


# ================================================================ get order (T-205)


def get_order(order_id: str, agent_id: str) -> dict[str, Any]:
    """GET /v1/orders/{order_id}: own orders only; lazy approval expiry applied."""
    with db.transaction() as conn:
        order = load_order(conn, order_id, for_update=True)
        if order is None:
            raise not_found(f"Order {order_id} does not exist.")
        if order["agent_id"] != agent_id:
            raise forbidden("This order belongs to another agent.")
        expire_approval_if_due(conn, order_id)
        return order_view(conn, order_id).dump()


# ================================================================ approval decisions (T-301)

COMMENT_MAX = 500


def _result(kind: str, order: dict[str, Any] | None = None,
            approval: dict[str, Any] | None = None) -> dict[str, Any]:
    """Small outcome dict for the approval pages. Never carries the token."""
    return {
        "result": kind,  # approved | declined | already_decided | expired | not_found
        "order_id": order["order_id"] if order else None,
        "order_status": order["status"] if order else None,
        "approval_status": approval["status"] if approval else None,
        "approver_name": approval["approver_name"] if approval else None,
        "expires_at": approval["expires_at"] if approval else None,
        "decided_at": approval["decided_at"] if approval else None,
    }


def decide_approval(token: str, decision: str, comment: str | None) -> dict[str, Any]:
    """POST /approve/{token}: approve or decline once (document 03, 8; document 05, 6 and 7.1).

    One transaction. Locks are taken order first, then approval, the same order as
    `expire_approval_if_due` and `get_order`, so concurrent decisions and expiry cannot
    deadlock. A token that is unknown, already used or past its deadline changes nothing
    (except that a due expiry is applied, as on any access: API-19).
    """
    if decision not in ("approve", "decline"):
        raise invalid_request("decision must be 'approve' or 'decline'.",
                              [{"field": "decision", "message": "approve or decline"}])
    text = (comment or "").strip()[:COMMENT_MAX] or None
    with db.transaction() as conn:
        found = find_approval_by_token(conn, token)
        if found is None:
            return _result("not_found")
        order_id = found["order_id"]
        order = load_order(conn, order_id, for_update=True)
        a = find_approval_by_token(conn, token, for_update=True)
        assert order is not None and a is not None
        if expire_approval_if_due(conn, order_id):
            return _result("expired", load_order(conn, order_id), load_approval(conn, order_id))
        if a["status"] != "pending" or order["status"] != "pending_approval":
            kind = "expired" if a["status"] == "expired" else "already_decided"
            return _result(kind, order, a)

        now = timeutil.now()
        approve = decision == "approve"
        new_order_status = "confirmed" if approve else "declined"
        db.q(conn, "UPDATE approvals SET status = :s, decided_at = :d, comment_untrusted = :c "
                   "WHERE approval_id = :a AND status = 'pending'",
             s="approved" if approve else "declined", d=timeutil.to_db(now), c=text,
             a=a["approval_id"])
        db.q(conn, "UPDATE orders SET status = :s, updated_at = :u WHERE order_id = :o",
             s=new_order_status, u=timeutil.to_db(now), o=order_id)

        trace, offer_id, name = order["trace_id"], order["offer_id"], a["approver_name"]
        events.append(
            conn, trace_id=trace, type="approval.decided", actor_type="approver",
            actor_id=name, audience="both", at=now,
            text=f"{name} {'approved' if approve else 'declined'} the order.",
            body={"order_id": order_id, "decision": "approved" if approve else "declined",
                  "has_comment": text is not None},
        )
        if approve:
            payload = (offers.load_offer(conn, offer_id) or {}).get("payload") or {}
            totals = payload.get("totals") or {}
            events.append(
                conn, trace_id=trace, type="order.confirmed", actor_type="gateway",
                audience="both",
                text=(f"Order {order_id} confirmed: {totals.get('excl_tax')} EUR excl. tax, "
                      f"delivery {payload.get('delivery_date')}. Approved by {name}."),
                body={"order_id": order_id, "status": "confirmed", "totals": totals},
            )
            commit_holds(conn, offer_id, trace)
        else:
            events.append(
                conn, trace_id=trace, type="order.closed", actor_type="gateway",
                audience="both",
                text=f"Order {order_id} closed as declined by {name}; its holds are released.",
                body={"order_id": order_id, "status": "declined"},
            )
            offers.release_holds(conn, offer_id, trace, "approval declined")
        from . import trust

        trust.note_change(conn, mandates.get_current(conn, order["agent_id"]), order_id, trace)
        return _result("approved" if approve else "declined",
                       load_order(conn, order_id), load_approval(conn, order_id))


def pending_approvals(conn: Connection) -> list[dict[str, Any]]:
    """Approvals still open (pending and not past their deadline), newest first. Read only:
    an approval past its deadline is left for lazy expiry and simply not listed."""
    rows = db.q(
        conn,
        "SELECT a.approval_id, a.order_id, a.approver_name, a.requested_at, a.expires_at, "
        "o.trace_id, o.offer_id, o.reference, c.name AS customer_name, g.name AS agent_name, "
        "f.payload_json FROM approvals a JOIN orders o ON o.order_id = a.order_id "
        "JOIN customers c ON c.customer_id = o.customer_id "
        "JOIN agents g ON g.agent_id = o.agent_id JOIN offers f ON f.offer_id = o.offer_id "
        "WHERE a.status = 'pending' AND o.status = 'pending_approval' AND a.expires_at > :n "
        "ORDER BY a.requested_at DESC",
        n=timeutil.to_db(timeutil.now()),
    ).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        payload = db.jload(d.pop("payload_json")) or {}
        d["requested_at"] = timeutil.from_db(d["requested_at"])
        d["expires_at"] = timeutil.from_db(d["expires_at"])
        d["totals"] = payload.get("totals") or {}
        d["delivery_date"] = payload.get("delivery_date")
        out.append(d)
    return out


__all__ = [
    "approval_token", "approval_url", "commit_holds", "decide_approval", "dry_run",
    "expire_approval_if_due", "extend_holds", "find_approval_by_token", "get_order",
    "load_approval", "load_order", "order_view", "pending_approvals", "place_order",
    "request_hash", "token_sha256",
]
