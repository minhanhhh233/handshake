"""Approval page (tasks T-301, T-302; document 03, section 8).

GET  /approve/{token}       order summary, cap meter, reasons, Approve / Decline form
POST /approve/{token}       decision (single use), then 303 to the result page
GET  /approve/{token}/done  outcome, read from the database (refresh-safe)

The token is the only credential. It is never rendered into the page (the form posts to
the page's own URL by omitting `action`), and every response carries no-store,
no-referrer and DENY framing headers. The agent's note and the approver's comment are
untrusted text and are only ever autoescaped.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from .. import db, offers, orders, timeutil
from .common import APPROVAL_HEADERS, render

router = APIRouter(include_in_schema=False)

TOKEN_MAX = 128  # real tokens are 43 characters; anything far longer is not worth hashing

ROLE_TEXT = {
    "requested": "The product the agent asked for",
    "alternate": "An alternate the agent listed itself",
    "substitute": "A substitute added by Valio Aimo",
}

OUTCOME_PARAM = {"already_decided", "expired"}


def _lookup(token: str) -> dict[str, Any] | None:
    """Everything the pages need, read only. Returns None for an unknown token."""
    if not token or len(token) > TOKEN_MAX:
        return None
    with db.connection() as conn:
        a = orders.find_approval_by_token(conn, token)
        if a is None:
            return None
        order = orders.load_order(conn, a["order_id"])
        assert order is not None
        offer = offers.load_offer(conn, order["offer_id"])
        assert offer is not None
        who = db.q(conn, "SELECT c.name AS customer_name, g.name AS agent_name FROM agents g "
                         "JOIN customers c ON c.customer_id = g.customer_id "
                         "WHERE g.agent_id = :a", a=order["agent_id"]).mappings().first()
    return {"approval": a, "order": order, "offer": offer, "payload": offer["payload"],
            "customer_name": who["customer_name"] if who else "",
            "agent_name": who["agent_name"] if who else "Your purchasing agent"}


def _dec(v: Any) -> Decimal | None:
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def cap_meter(reasons: list[dict[str, Any]], total_excl: Any) -> dict[str, Any] | None:
    """Geometry for the cap meter, or None when the spend cap is not a reason."""
    r = next((r for r in reasons if r.get("code") == "spend_cap_exceeded"), None)
    if r is None:
        return None
    d = r.get("details") or {}
    total, cap = _dec(d.get("total_excl_tax", total_excl)), _dec(d.get("cap"))
    if total is None or cap is None or total <= 0 or cap <= 0 or total <= cap:
        return None
    over = total - cap
    cap_pct = float(cap / total * 100)
    # A small overage (10.00 of 1010.00 is 1 %) would be a hairline; keep at least 4 % of
    # the bar so it reads at phone width. The sentence carries the exact figures.
    cap_pct = min(cap_pct, 96.0)
    return {"total": f"{total:.2f}", "cap": f"{cap:.2f}", "over": f"{over:.2f}",
            "cap_pct": round(cap_pct, 2), "over_pct": round(100 - cap_pct, 2),
            "currency": d.get("currency") or "EUR"}


def _lines(payload: dict[str, Any]) -> list[dict[str, Any]]:
    names = {ln.get("product_id"): ln.get("name") for ln in payload.get("lines") or []}
    out = []
    for ln in payload.get("lines") or []:
        q, base = ln.get("quantity") or {}, ln.get("base_quantity") or {}
        av = ln.get("availability") or {}
        role = ln.get("role") or "requested"
        role_text = ROLE_TEXT.get(role, role)
        if role == "substitute" and ln.get("substitute_for"):
            sub = names.get(ln["substitute_for"]) or ln["substitute_for"]
            role_text = f"A substitute added by Valio Aimo for {sub}"
        sales_unit = "units" if q.get("unit") == "SALES_UNIT" else (q.get("unit") or "")
        out.append({
            "name": ln.get("name") or ln.get("product_id"),
            "product_id": ln.get("product_id"),
            "qty": q.get("value"), "sales_unit": sales_unit,
            "base_qty": base.get("value") if base.get("unit") in ("L", "KG") else None,
            "base_unit": base.get("unit"),
            "unit_price": ln.get("unit_price_excl_tax"),
            "total_excl": ln.get("line_total_excl_tax"),
            "total_incl": ln.get("line_total_incl_tax"),
            "role": role, "role_text": role_text,
            "assurance": av.get("assurance"), "available_date": av.get("available_date"),
            "logistic_model": av.get("logistic_model"),
        })
    return out


def _closed_view(info: dict[str, Any], kind: str | None = None) -> dict[str, Any]:
    """State of a request that can no longer be decided (or was just decided)."""
    a, order = info["approval"], info["order"]
    status = a["status"]
    if status == "pending" and a["expires_at"] <= timeutil.now():
        status = "expired"  # due but not yet applied; shown as expired, nothing changes
    return {"status": status, "kind": kind, "approver_name": a["approver_name"],
            "decided_at": a["decided_at"], "expires_at": a["expires_at"],
            "order_id": order["order_id"], "order_status": order["status"],
            "comment": a["comment_untrusted"], "totals": info["payload"].get("totals") or {},
            "delivery_date": info["payload"].get("delivery_date"),
            "customer_name": info["customer_name"], "agent_name": info["agent_name"]}


def _not_found() -> Response:
    return render("approve_missing.html", status=404, headers=APPROVAL_HEADERS,
                  title="Link not valid")


@router.get("/approve/{token}")
def approval_page(token: str) -> Response:
    info = _lookup(token)
    if info is None:
        return _not_found()
    a, order, payload = info["approval"], info["order"], info["payload"]
    closed = (a["status"] != "pending" or order["status"] != "pending_approval"
              or a["expires_at"] <= timeutil.now())
    if closed:
        view = _closed_view(info, "already_decided")
        return render("approve_result.html", headers=APPROVAL_HEADERS, v=view,
                      title="Approval request closed")
    reasons = order["reasons"]
    totals = payload.get("totals") or {}
    ctx = {
        "title": "Approve this order?",
        "customer_name": info["customer_name"], "agent_name": info["agent_name"],
        "approver_name": a["approver_name"],
        "reasons": reasons,
        "other_reasons": [r for r in reasons if r.get("code") != "spend_cap_exceeded"],
        "meter": cap_meter(reasons, totals.get("excl_tax")),
        "lines": _lines(payload), "totals": totals,
        "delivery_date": payload.get("delivery_date"),
        "fulfilment": payload.get("fulfilment") or [],
        "offer_id": payload.get("offer_id"), "offer_expires_at": payload.get("expires_at"),
        "mandate_version": payload.get("mandate_version"),
        "reference": order["reference"], "note": order["note_untrusted"],
        "expires_at": a["expires_at"], "requested_at": a["requested_at"],
        "comment_max": orders.COMMENT_MAX,
    }
    return render("approve.html", headers=APPROVAL_HEADERS, **ctx)


@router.post("/approve/{token}")
def approval_decide(token: str, request: Request, decision: str = Form(default=""),
                    comment: str | None = Form(default=None)) -> Response:
    if not token or len(token) > TOKEN_MAX:
        return _not_found()
    if decision not in ("approve", "decline"):
        info = _lookup(token)
        if info is None:
            return _not_found()
        return RedirectResponse(f"/approve/{token}", status_code=303, headers=APPROVAL_HEADERS)
    result = orders.decide_approval(token, decision, comment)
    if result["result"] == "not_found":
        return _not_found()
    url = f"/approve/{token}/done"
    if result["result"] in OUTCOME_PARAM:
        url += f"?outcome={result['result']}"
    return RedirectResponse(url, status_code=303, headers=APPROVAL_HEADERS)


@router.get("/approve/{token}/done")
def approval_done(token: str, outcome: str | None = None) -> Response:
    info = _lookup(token)
    if info is None:
        return _not_found()
    kind = outcome if outcome in OUTCOME_PARAM else None
    view = _closed_view(info, kind)
    if view["status"] == "pending":  # nothing decided yet: back to the form
        return RedirectResponse(f"/approve/{token}", status_code=303, headers=APPROVAL_HEADERS)
    return render("approve_result.html", headers=APPROVAL_HEADERS, v=view,
                  title="Approval request")
