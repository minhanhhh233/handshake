"""Operator console (task T-303; document 03, section 8; A-09).

GET /console?key=<ADMIN_KEY>  checks the key, sets an HttpOnly SameSite=Strict cookie (an HMAC
                              of the key, path /console) and 303-redirects to /console so the
                              key leaves the address bar and history.
GET /console                  recent events and pending approvals (cookie).
GET /console/live             the live part as an HTML fragment, polled every 3 seconds.

QR codes and approval links are shown only when DEMO_MODE is true (A-09).
"""

from __future__ import annotations

import hashlib
import hmac
import io

import segno
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from .. import config, db, events, orders
from .common import CONSOLE_HEADERS, render

router = APIRouter(include_in_schema=False)

COOKIE = "hs_console"
EVENT_LIMIT = 40


def _session_value(admin_key: str) -> str:
    """Cookie value: an HMAC of the admin key, so the key itself is never stored in the
    browser. Changing ADMIN_KEY ends every console session."""
    return hmac.new(admin_key.encode("utf-8"), b"handshake-console-v1",
                    hashlib.sha256).hexdigest()


def _key_ok(supplied: str | None) -> bool:
    expected = config.get_settings().admin_key
    return bool(expected and supplied) and hmac.compare_digest(supplied, expected)


def _cookie_ok(request: Request) -> bool:
    expected = config.get_settings().admin_key
    got = request.cookies.get(COOKIE)
    return bool(expected and got) and hmac.compare_digest(got, _session_value(expected))


def _forbidden() -> Response:
    return render("console_forbidden.html", status=403, headers=CONSOLE_HEADERS,
                  title="Console")


def qr_svg(url: str) -> str:
    """Inline SVG QR code (no width/height, so CSS sizes it)."""
    out = io.BytesIO()
    segno.make(url, error="m").save(out, kind="svg", scale=1, border=2, dark="#14324A",
                                    light="#FFFFFF", xmldecl=False, svgns=True,
                                    omitsize=True, svgclass="qr")
    return out.getvalue().decode("utf-8")


def _live_context() -> dict:
    demo = config.get_settings().demo_mode
    with db.connection() as conn:
        recent = events.recent(conn, limit=EVENT_LIMIT)
        pending = orders.pending_approvals(conn)
    for p in pending:
        if demo:
            p["url"] = orders.approval_url(p["approval_id"])
            p["qr"] = qr_svg(p["url"])
    return {"events": recent, "pending": pending, "demo": demo}


@router.get("/console")
def console(request: Request, key: str | None = None) -> Response:
    if key is not None:
        if not _key_ok(key):
            return _forbidden()
        resp = RedirectResponse("/console", status_code=303, headers=CONSOLE_HEADERS)
        secure = config.get_settings().public_base_url.startswith("https://")
        resp.set_cookie(COOKIE, _session_value(config.get_settings().admin_key),
                        path="/console", httponly=True, samesite="strict", secure=secure,
                        max_age=12 * 3600)
        return resp
    if not _cookie_ok(request):
        return _forbidden()
    return render("console.html", headers=CONSOLE_HEADERS, title="Handshake console",
                  **_live_context())


@router.get("/console/live")
def console_live(request: Request) -> Response:
    if not _cookie_ok(request):
        return Response("forbidden", status_code=403, headers=CONSOLE_HEADERS,
                        media_type="text/plain")
    return render("_console_live.html", headers=CONSOLE_HEADERS, **_live_context())
