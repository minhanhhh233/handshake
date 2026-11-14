"""Public landing page and guided demo (document 03, section 8; document 09, section 6a).

GET  /                         landing page: what Handshake is, links, three demo buttons
POST /demo/run                 form field `scenario`; runs it server-side, 303 to the run page
GET  /demo/runs/{trace_id}?k=  steps of that run; for the example order, the approval link and
                               QR of THAT order while it is pending, then the receipt link
No admin key, console or other order's approval link is reachable from these routes.
"""

from __future__ import annotations

import logging

from pathlib import Path

from fastapi import APIRouter, Form, Request
from jinja2 import Environment, FileSystemLoader
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from .. import public_demo, sandbox, timeutil
from ..config import get_settings
from ..ids import is_id
from .common import APPROVAL_HEADERS, render
from .console import qr_svg

log = logging.getLogger("handshake.public")
router = APIRouter(include_in_schema=False)

#: Script policy for the landing and run pages ONLY: one self-hosted file (/static/site.js),
#: no inline script, fetch only to this origin. Approval and receipt pages stay script-free.
_SCRIPT_CSP = (
    "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; "
    "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)
#: Landing page: no secrets; short public caching is fine.
PUBLIC_HEADERS = {
    "Cache-Control": "public, max-age=60",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": _SCRIPT_CSP,
}
#: Run page: shows the visitor's own approval link, so the approval page's caching and
#: referrer rules apply; the only difference is the self-hosted script.
RUN_HEADERS = {**APPROVAL_HEADERS, "Content-Security-Policy": _SCRIPT_CSP}
_STATIC = Path(__file__).resolve().parent / "static"
_SITE_JS = (_STATIC / "site.js").read_text(encoding="utf-8")
#: Plain-text templates (llms.txt): no HTML escaping; values are server configuration only.
_TEXT_ENV = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parent / "templates")),
                        autoescape=False, keep_trailing_newline=True)

#: Discovery rows on the landing page: public endpoints an agent can read before it has a key.
DISCOVERY = [
    {"method": "GET", "path": "/.well-known/handshake.json", "std": "Discovery",
     "desc": "API base, signing scheme, capabilities, authentication and time zone.",
     "ct": "application/json"},
    {"method": "GET", "path": "/.well-known/jwks.json", "std": "JWKS",
     "desc": "Public Ed25519 keys that verify offers and receipts.", "ct": "application/json"},
    {"method": "GET", "path": "/openapi.json", "std": "OpenAPI",
     "desc": "Machine-readable description of every operation.", "ct": "application/json"},
    {"method": "GET", "path": "/docs", "std": "OpenAPI",
     "desc": "Interactive API documentation generated from the description.", "ct": "text/html"},
    {"method": "GET", "path": "/llms.txt", "std": "llms.txt",
     "desc": "The gateway described for language models: rules, endpoints, links.",
     "ct": "text/plain"},
    {"method": "GET", "path": "/v1/products?category=10400&limit=5", "std": "OpenAPI",
     "desc": "Product search without a key: attributes only, no prices or stock.",
     "ct": "application/json"},
    {"method": "GET", "path": "/v1/health", "std": "OpenAPI",
     "desc": "Liveness of the gateway and its database.", "ct": "application/json"},
    {"method": "POST", "path": "/v1/offers", "std": "JWS",
     "desc": "Signed, time-limited offer. Needs an agent key.", "ct": "application/json"},
]


@router.get("/")
def landing() -> Response:
    return render("landing.html", headers=PUBLIC_HEADERS, title="Handshake",
                  base=get_settings().public_base_url, kid=get_settings().signing_kid,
                  limit=public_demo.RUNS_PER_WINDOW,
                  window_min=public_demo.WINDOW_SECONDS // 60, stats=public_demo.site_stats(),
                  discovery=[d | {"href": d["path"]} for d in DISCOVERY],
                  plain_excludes=public_demo.PLAIN_MILK_EXCLUDES,
                  shortlist=public_demo.SHORTLIST,
                  max_searches=public_demo.MAX_SEARCHES_PER_SESSION,
                  live=_live_status(), live_examples=_live_examples(),
                  personas=sandbox.PERSONAS, roles=sandbox.ROLES,
                  roles_ready=sandbox.ROLES_READY,
                  live_max=_live_max())


def _live_status() -> dict:
    from .. import live_demo

    try:
        return live_demo.status()
    except Exception:  # noqa: BLE001 - the landing page must render even if this fails
        log.exception("live demo status failed")
        return {"available": False, "code": "error"}


def _live_examples() -> list[str]:
    from .. import live_demo

    return live_demo.EXAMPLES


def _live_max() -> int:
    from .. import live_demo

    return live_demo.MAX_PROMPT_CHARS


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
def demo_view(request: Request, trace_id: str, k: str | None = None) -> Response:
    if not is_id(trace_id, "trc") or not public_demo.check_run_token(trace_id, k):
        return _not_found()
    view = public_demo.run_view(trace_id)
    if view is None:
        return _not_found()
    # A run made in a sandbox is visible only from that sandbox (stage 1 security review).
    if view.get("sandbox_id") and view["sandbox_id"] != sandbox.parse_cookie(
            request.cookies.get(sandbox.COOKIE)):
        return _not_found()
    order = view.get("order") or {}
    approval_url = order.get("approval_url")
    pending = order.get("status") == "pending_approval"
    return render("demo_run.html", headers=RUN_HEADERS, title="Handshake demo run",
                  view=view, order=order, approval_url=approval_url,
                  qr=qr_svg(approval_url) if approval_url else None,
                  refresh=pending, now=timeutil.now())


def _not_found() -> Response:
    return render("demo_message.html", status=404, headers=APPROVAL_HEADERS,
                  title="Demo run not found", heading="This demo run link is not valid.",
                  body="Start a new run from the start page.")


@router.get("/static/site.js")
def site_js() -> Response:
    return Response(_SITE_JS, media_type="application/javascript; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=300",
                             "X-Content-Type-Options": "nosniff"})


@router.get("/llms.txt")
def llms_txt() -> Response:
    s = get_settings()
    text = _TEXT_ENV.get_template("llms.txt").render(base=s.public_base_url, kid=s.signing_kid,
                                               offer_minutes=s.offer_ttl_seconds // 60)
    return PlainTextResponse(text, headers={"Cache-Control": "public, max-age=300",
                                            "X-Content-Type-Options": "nosniff"})


@router.post("/demo/playground/{step}")
async def playground(step: str, request: Request) -> Response:
    """One playground card: run that example-order step as the public-demo agent (JSON)."""
    if not request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse({"code": "invalid_request", "message": "Send JSON."}, status_code=415)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    trace_id, token = body.get("trace_id"), body.get("k")
    if trace_id is not None and not (isinstance(trace_id, str) and is_id(trace_id, "trc")):
        return JSONResponse({"code": "unknown_session", "message": "Invalid session."},
                            status_code=404)
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    try:
        from starlette.concurrency import run_in_threadpool

        out = await run_in_threadpool(public_demo.playground, step,
                                      public_demo.client_ip(request), trace_id,
                                      token if isinstance(token, str) else None)
    except public_demo.RateLimited as e:
        return JSONResponse({"code": "rate_limited", "message": (
            f"Each visitor can start {public_demo.RUNS_PER_WINDOW} demo runs every "
            f"{public_demo.WINDOW_SECONDS // 60} minutes. Try again in about "
            f"{max(1, round(e.retry_after / 60))} minute(s).")}, status_code=429,
            headers={**headers, "Retry-After": str(e.retry_after)})
    except public_demo.PlaygroundError as e:
        return JSONResponse({"code": e.code, "message": e.message}, status_code=e.status,
                            headers=headers)
    except public_demo.DemoUnavailable:
        return JSONResponse({"code": "demo_unavailable", "message": "The demo is not set up."},
                            status_code=503, headers=headers)
    return JSONResponse(out, headers=headers)


# ---------------------------------------------------------------- live agent demo


_LIVE_MESSAGES = {
    "no_api_key": "The live agent is not available on this server. Use a scripted demo below.",
    "budget": "Today's budget for the live agent is used up. The scripted demos below still work.",
    "busy": "The live agent is busy with other visitors. Try again in a minute, or use a "
            "scripted demo below.",
}


@router.post("/demo/live")
async def live_start(request: Request) -> Response:
    """Start a live agent run (document 03, section 8). JSON {"prompt": "..."}."""
    from .. import live_demo

    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if not request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse({"code": "invalid_request", "message": "Send JSON."}, status_code=415,
                            headers=headers)
    try:
        body = await request.json()
    except ValueError:
        body = None
    body = body if isinstance(body, dict) else {}
    prompt = live_demo.clean_prompt(body.get("prompt"))
    if prompt is None:
        return JSONResponse({"code": "invalid_prompt", "message": (
            f"Write a purchasing request of 1 to {live_demo.MAX_PROMPT_CHARS} characters.")},
            status_code=400, headers=headers)
    persona = body.get("persona", "hotel")
    if persona not in sandbox.PERSONAS:
        return JSONResponse({"code": "unknown_persona", "message": "Choose a persona."},
                            status_code=400, headers=headers)
    sb = sandbox.active(sandbox.parse_cookie(request.cookies.get(sandbox.COOKIE)))
    if sb is None:
        return JSONResponse({"code": "no_sandbox", "message": (
            "Your demo sandbox has expired. Reload the page to start a new one.")},
            status_code=409, headers=headers)
    try:
        from starlette.concurrency import run_in_threadpool

        trace_id = await run_in_threadpool(lambda: live_demo.start(
            prompt, public_demo.client_ip(request), persona=persona,
            sandbox_id=sb["sandbox_id"]))
    except public_demo.RateLimited as e:
        return JSONResponse({"code": "rate_limited", "message": (
            f"Each visitor can start {live_demo.RUNS_PER_WINDOW} live runs every "
            f"{live_demo.WINDOW_SECONDS // 60} minutes. Try again in about "
            f"{max(1, round(e.retry_after / 60))} minute(s), or use a scripted demo below.")},
            status_code=429, headers={**headers, "Retry-After": str(e.retry_after)})
    except live_demo.LiveUnavailable as e:
        return JSONResponse({"code": e.code, "message": _LIVE_MESSAGES.get(e.code, e.message)},
                            status_code=503, headers=headers)
    except public_demo.DemoUnavailable:
        return JSONResponse({"code": "demo_unavailable", "message": "The demo is not set up."},
                            status_code=503, headers=headers)
    return JSONResponse({"trace_id": trace_id, "k": public_demo.run_token(trace_id)},
                        status_code=202, headers=headers)


@router.get("/demo/live/{trace_id}")
def live_poll(request: Request, trace_id: str, k: str | None = None, after: int = 0
              ) -> Response:
    """Steps of a live run after index `after`, its state and the visitor's own order."""
    from .. import live_demo

    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
               "Referrer-Policy": "no-referrer"}
    if not is_id(trace_id, "trc") or not public_demo.check_run_token(trace_id, k):
        return JSONResponse({"code": "not_found", "message": "Unknown run."}, status_code=404,
                            headers=headers)
    sid = sandbox.parse_cookie(request.cookies.get(sandbox.COOKIE))
    v = live_demo.view(trace_id, max(0, after), sandbox_id=sid)
    if v is None:
        return JSONResponse({"code": "not_found", "message": "Unknown run."}, status_code=404,
                            headers=headers)
    v["run_url"] = f"/demo/runs/{trace_id}?k={k}"
    return JSONResponse(v, headers=headers)


# ---------------------------------------------------------------- personas and sandboxes


_JSON_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                 "Referrer-Policy": "no-referrer"}


def _with_cookie(resp: Response, sb: dict, is_new: bool) -> Response:
    if is_new:
        resp.set_cookie(sandbox.COOKIE, sandbox.cookie_value(sb["sandbox_id"]),
                        max_age=int(sandbox.TTL.total_seconds()), path="/demo", httponly=True,
                        samesite="lax",
                        secure=get_settings().public_base_url.startswith("https://"))
    return resp


def _sandbox_json(sb: dict, card: dict) -> dict:
    from .. import scenarios

    return {"sandbox": {"expires_at": timeutil.iso(sb["expires_at"])},
            "personas": [{"slug": k, "label": v["label"]} for k, v in sandbox.PERSONAS.items()],
            "card": card, "scenarios": scenarios.for_persona(card["persona"])}


@router.get("/demo/sandbox")
def sandbox_state(request: Request, persona: str = "hotel") -> Response:
    """The visitor's sandbox (created or renewed here) and the mandate card of a persona."""
    if persona not in sandbox.PERSONAS and persona != "approver":
        return JSONResponse({"code": "unknown_persona", "message": "Unknown persona."},
                            status_code=400, headers=_JSON_HEADERS)
    if persona == "approver":
        try:
            sb, is_new = sandbox.ensure(request.cookies.get(sandbox.COOKIE),
                                        public_demo.client_ip(request))
        except sandbox.SandboxError as e:
            return JSONResponse({"code": e.code, "message": e.message}, status_code=e.status,
                                headers=_JSON_HEADERS)
        body = {"sandbox": {"expires_at": timeutil.iso(sb["expires_at"])},
                "card": {"persona": "approver", "label": "Approver"},
                "inbox": sandbox.inbox(sb["sandbox_id"])}
        return _with_cookie(JSONResponse(body, headers=_JSON_HEADERS), sb, is_new)
    try:
        sb, is_new = sandbox.ensure(request.cookies.get(sandbox.COOKIE),
                                    public_demo.client_ip(request))
        card = sandbox.card(sb["sandbox_id"], persona)
    except sandbox.SandboxError as e:
        return JSONResponse({"code": e.code, "message": e.message}, status_code=e.status,
                            headers=_JSON_HEADERS)
    except public_demo.DemoUnavailable:
        return JSONResponse({"code": "demo_unavailable", "message": "The demo is not set up."},
                            status_code=503, headers=_JSON_HEADERS)
    return _with_cookie(JSONResponse(_sandbox_json(sb, card), headers=_JSON_HEADERS), sb,
                        is_new)


@router.post("/demo/sandbox/cap")
async def sandbox_cap(request: Request) -> Response:
    """Change the per-order cap of the active persona's mandate in this sandbox."""
    if not request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse({"code": "invalid_request", "message": "Send JSON."}, status_code=415,
                            headers=_JSON_HEADERS)
    try:
        body = await request.json()
    except ValueError:
        body = None
    body = body if isinstance(body, dict) else {}
    persona = body.get("persona")
    if persona not in sandbox.BUYERS:
        return JSONResponse({"code": "unknown_persona", "message": "Choose a buyer persona."},
                            status_code=400, headers=_JSON_HEADERS)
    sb = sandbox.active(sandbox.parse_cookie(request.cookies.get(sandbox.COOKIE)))
    if sb is None:
        return JSONResponse({"code": "no_sandbox", "message": (
            "Your demo sandbox has expired. Reload the page to start a new one.")},
            status_code=409, headers=_JSON_HEADERS)
    try:
        from starlette.concurrency import run_in_threadpool

        card = await run_in_threadpool(sandbox.set_cap, sb["sandbox_id"], persona,
                                       body.get("cap"))
    except sandbox.SandboxError as e:
        return JSONResponse({"code": e.code, "message": e.message}, status_code=e.status,
                            headers=_JSON_HEADERS)
    return JSONResponse(_sandbox_json(sb, card), headers=_JSON_HEADERS)


# ---------------------------------------------------------------- scenarios and inbox (stage 2)


@router.post("/demo/scenario")
async def scenario_start(request: Request) -> Response:
    """Start a one-click scripted scenario for a persona. JSON {"id", "persona"}."""
    from .. import live_demo, scenarios

    if not request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse({"code": "invalid_request", "message": "Send JSON."}, status_code=415,
                            headers=_JSON_HEADERS)
    try:
        body = await request.json()
    except ValueError:
        body = None
    body = body if isinstance(body, dict) else {}
    sid, persona = body.get("id"), body.get("persona")
    if not isinstance(sid, str) or not isinstance(persona, str):
        return JSONResponse({"code": "invalid_request", "message": "Choose a scenario."},
                            status_code=400, headers=_JSON_HEADERS)
    sb = sandbox.active(sandbox.parse_cookie(request.cookies.get(sandbox.COOKIE)))
    if sb is None:
        return JSONResponse({"code": "no_sandbox", "message": (
            "Your demo sandbox has expired. Reload the page to start a new one.")},
            status_code=409, headers=_JSON_HEADERS)
    try:
        from starlette.concurrency import run_in_threadpool

        trace_id = await run_in_threadpool(lambda: scenarios.start(
            sid, persona, sb["sandbox_id"], public_demo.client_ip(request)))
    except public_demo.RateLimited as e:
        return JSONResponse({"code": "rate_limited", "message": (
            f"Each visitor can run {scenarios.RUNS_PER_WINDOW} scenarios every "
            f"{scenarios.WINDOW_SECONDS // 60} minutes. Try again in about "
            f"{max(1, round(e.retry_after / 60))} minute(s).")}, status_code=429,
            headers={**_JSON_HEADERS, "Retry-After": str(e.retry_after)})
    except live_demo.LiveUnavailable as e:
        return JSONResponse({"code": e.code, "message": e.message},
                            status_code=400 if e.code == "invalid_scenario" else 503,
                            headers=_JSON_HEADERS)
    except public_demo.DemoUnavailable:
        return JSONResponse({"code": "demo_unavailable", "message": "The demo is not set up."},
                            status_code=503, headers=_JSON_HEADERS)
    return JSONResponse({"trace_id": trace_id, "k": public_demo.run_token(trace_id)},
                        status_code=202, headers=_JSON_HEADERS)


@router.get("/demo/approvals")
def approvals_inbox(request: Request) -> Response:
    """The Approver role's inbox: pending approvals of the caller's sandbox only."""
    sb = sandbox.active(sandbox.parse_cookie(request.cookies.get(sandbox.COOKIE)))
    if sb is None:
        return JSONResponse({"code": "no_sandbox", "message": (
            "Your demo sandbox has expired. Reload the page to start a new one.")},
            status_code=409, headers=_JSON_HEADERS)
    return JSONResponse({"items": sandbox.inbox(sb["sandbox_id"])}, headers=_JSON_HEADERS)
