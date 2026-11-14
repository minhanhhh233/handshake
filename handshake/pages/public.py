"""Public landing page and guided demo (document 03, section 8; document 09, section 6a).

GET  /                         landing page: what Handshake is, links, three demo buttons
POST /demo/run                 form field `scenario`; runs it server-side, 303 to the run page
GET  /demo/runs/{trace_id}?k=  steps of that run; for the example order, the approval link and
                               QR of THAT order while it is pending, then the receipt link
No admin key, console or other order's approval link is reachable from these routes.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from .. import public_demo, timeutil
from ..config import get_settings
from ..ids import is_id
from .common import APPROVAL_HEADERS, render
from .console import qr_svg

log = logging.getLogger("handshake.public")
router = APIRouter(include_in_schema=False)

#: Landing page: no secrets, no scripts; short public caching is fine.
PUBLIC_HEADERS = {
    "Cache-Control": "public, max-age=60",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
}


@router.get("/")
def landing() -> Response:
    return render("landing.html", headers=PUBLIC_HEADERS, title="Handshake",
                  base=get_settings().public_base_url, kid=get_settings().signing_kid,
                  limit=public_demo.RUNS_PER_WINDOW,
                  window_min=public_demo.WINDOW_SECONDS // 60)


@router.post("/demo/run")
def demo_run(request: Request, scenario: str = Form(...)) -> Response:
    if scenario not in public_demo.SCENARIOS:
        return render("demo_message.html", status=400, headers=APPROVAL_HEADERS,
                      title="Unknown demo", heading="That demo does not exist.",
                      body="Choose one of the three demos on the start page.")
    try:
        trace_id = public_demo.run(scenario, public_demo.client_ip(request))
    except public_demo.RateLimited as e:
        resp = render("demo_message.html", status=429, headers=APPROVAL_HEADERS,
                      title="Too many demo runs", heading="That is enough demo runs for now.",
                      body=f"Each visitor can start {public_demo.RUNS_PER_WINDOW} runs every "
                           f"{public_demo.WINDOW_SECONDS // 60} minutes. Try again in about "
                           f"{max(1, round(e.retry_after / 60))} minute(s).")
        resp.headers["Retry-After"] = str(e.retry_after)
        return resp
    except public_demo.DemoUnavailable:
        log.warning("public demo is not seeded")
        return render("demo_message.html", status=503, headers=APPROVAL_HEADERS,
                      title="Demo unavailable", heading="The demo is not set up on this server.",
                      body="The operator needs to run scripts/seed_demo.py.")
    return RedirectResponse(f"/demo/runs/{trace_id}?k={public_demo.run_token(trace_id)}",
                            status_code=303)


@router.get("/demo/runs/{trace_id}")
def demo_view(trace_id: str, k: str | None = None) -> Response:
    if not is_id(trace_id, "trc") or not public_demo.check_run_token(trace_id, k):
        return _not_found()
    view = public_demo.run_view(trace_id)
    if view is None:
        return _not_found()
    order = view.get("order") or {}
    approval_url = order.get("approval_url")
    pending = order.get("status") == "pending_approval"
    return render("demo_run.html", headers=APPROVAL_HEADERS, title="Handshake demo run",
                  view=view, order=order, approval_url=approval_url,
                  qr=qr_svg(approval_url) if approval_url else None,
                  refresh=pending, now=timeutil.now())


def _not_found() -> Response:
    return render("demo_message.html", status=404, headers=APPROVAL_HEADERS,
                  title="Demo run not found", heading="This demo run link is not valid.",
                  body="Start a new run from the start page.")
