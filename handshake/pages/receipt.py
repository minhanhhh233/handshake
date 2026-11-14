"""Receipt page (task T-403; document 03, section 8; document 05, section 7.4).

GET /receipts/{order_id}?t=<receipt_token>   buyer view (events with audience both)
GET /receipts/{order_id}?key=<ADMIN_KEY>     sets an HttpOnly SameSite=Strict cookie (an HMAC of
                                             the key, path /receipts) and 303-redirects to
                                             /receipts/{order_id} without the query
GET /receipts/{order_id}                     Valio Aimo view with that cookie: all events plus
                                             upstream snapshot references

A wrong or missing token, a wrong key and an unknown order all give the same 404 page, so the
page does not reveal whether an order exists. Neither the token nor the key is rendered into
the page; responses carry the approval page's no-store, no-referrer, DENY and no-script CSP.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from .. import config, db, orders, receipts
from ..errors import HandshakeError
from ..ids import is_id
from .common import APPROVAL_HEADERS, render
from .console import _key_ok, _session_value

router = APIRouter(include_in_schema=False)

COOKIE = "hs_receipts"
TOKEN_MAX = 128


def _cookie_ok(request: Request) -> bool:
    expected = config.get_settings().admin_key
    got = request.cookies.get(COOKIE)
    return bool(expected and got) and hmac.compare_digest(got, _session_value(expected))


def _not_found() -> Response:
    return render("receipt_missing.html", status=404, headers=APPROVAL_HEADERS,
                  title="Receipt not found")


def _token_ok(order_id: str, token: str | None) -> bool:
    if not token or len(token) > TOKEN_MAX:
        return False
    with db.connection() as conn:
        order = orders.load_order(conn, order_id)
    stored = order["receipt_token"] if order else None
    # Compare against a dummy when the order is missing so timing reveals nothing.
    return hmac.compare_digest(token.encode(), (stored or "\0" * 43).encode()) and bool(stored)


@router.get("/receipts/{order_id}")
def receipt_page(order_id: str, request: Request, t: str | None = None,
                 key: str | None = None) -> Response:
    if not is_id(order_id, "ord"):
        return _not_found()
    if key is not None:
        if not _key_ok(key):
            return _not_found()
        resp = RedirectResponse(f"/receipts/{order_id}", status_code=303,
                                headers=APPROVAL_HEADERS)
        settings = config.get_settings()
        resp.set_cookie(COOKIE, _session_value(settings.admin_key), path="/receipts",
                        httponly=True, samesite="strict",
                        secure=settings.public_base_url.startswith("https://"),
                        max_age=12 * 3600)
        return resp
    if _cookie_ok(request):
        audience = "aimo"
    elif _token_ok(order_id, t):
        audience = "buyer"
    else:
        return _not_found()
    try:
        with db.connection() as conn:
            view = receipts.receipt_view(conn, order_id, audience)
    except HandshakeError:
        return _not_found()
    title = "Receipt" if audience == "buyer" else "Receipt, Valio Aimo view"
    return render("receipt.html", headers=APPROVAL_HEADERS, title=title, v=view)
