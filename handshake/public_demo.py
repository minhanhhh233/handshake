"""Public guided demo on the landing page (document 09, section 6a).

Runs the scripted scenarios server-side for anonymous visitors, as dedicated public-demo agents,
through the same internal functions the API uses (no Claude API calls, no HTTP loopback):

- example order: mandate, search, earliest deliverable date, signed offer (signature verified
  against the published key), dry run, order. The order needs approval (cap 1000.00 EUR), so the
  run page shows the approval link and QR for THIS order only; the visitor approves it.
- milk-allergy guard: the allergy agent's offer request is refused by the mandate.
- anonymous search: catalogue search without credentials (no prices, no stock).

Storage: each run is a trace. Steps are `demo.step` events and the visitor limit uses `demo.run`
events (both audience aimo_only), so no schema change is needed and all state stays in MariaDB.
The run page is reachable only with a token that is an HMAC of the trace ID (keyed with
ADMIN_KEY, which is never exposed). Visitor IPs are stored only as keyed hashes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import date, timedelta
from typing import Any

from fastapi import Request

from . import catalog, db, events, mandates, offers, orders, receipts, signing, timeutil, upstream
from .config import get_settings
from .errors import HandshakeError
from .ids import new_id
from .models import OfferRequest, OrderRequest

PUBLIC_CUSTOMER = "Handshake public demo"
AGENTS = {"order": "Public demo agent", "allergy": "Public demo agent (milk allergy)"}
SCENARIOS = ("example_order", "allergy_guard", "anonymous_search")
RUNS_PER_WINDOW = 5
WINDOW_SECONDS = 600
APPROVAL_TTL_SECONDS = 1800  # public-demo orders expire after 30 minutes
SHORTLIST = ["464306", "464398", "464201", "465039", "469363", "462775", "464179", "463038"]
PLAIN_MILK_EXCLUDES = ["sour", "piimä", "coffee", "kahvi", "latte", "portion", "annos", "café",
                       "cafe"]


class RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


class DemoUnavailable(Exception):
    """The public demo parties are not seeded (scripts/seed_demo.py)."""


# ---------------------------------------------------------------- landing page figures

#: Tools an agent gets (document 07, section 2); tests/test_mcp_server.py asserts exactly these 8.
AGENT_TOOLS = 8
_stats_cache: tuple[float, dict[str, int]] | None = None


def site_stats() -> dict[str, int]:
    """Real figures for the landing page, cached in-process for 10 minutes."""
    global _stats_cache
    import time
    from typing import get_args

    from .models import Outcome

    now = time.monotonic()
    if _stats_cache is None or now - _stats_cache[0] > 600:
        with db.connection() as conn:
            products = int(db.q(conn, "SELECT COUNT(*) FROM products").scalar_one())
        _stats_cache = (now, {
            "products": products, "tools": AGENT_TOOLS, "outcomes": len(get_args(Outcome)),
            "offer_minutes": get_settings().offer_ttl_seconds // 60})
    return _stats_cache[1]


# ---------------------------------------------------------------- identity, tokens, limits


def _hmac(label: str, value: str) -> str:
    key = get_settings().admin_key.encode()
    if not key:
        raise DemoUnavailable("ADMIN_KEY is not set")
    digest = hmac.new(key, f"{label}:{value}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def run_token(trace_id: str) -> str:
    return _hmac("demo-run", trace_id)[:32]


def check_run_token(trace_id: str, token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(run_token(trace_id), str(token))


def client_ip(request: Request) -> str:
    """Visitor IP. Behind Modal's proxy the right-most X-Forwarded-For entry is the one the
    proxy added (entries to its left can be set by the client)."""
    xff = request.headers.get("x-forwarded-for", "")
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    if parts:
        return parts[-1]
    return request.client.host if request.client else "unknown"


def ip_hash(ip: str) -> str:
    return _hmac("demo-ip", ip)[:24]


def _agent(conn: Any, slug: str) -> dict[str, Any]:
    row = db.q(conn, "SELECT a.agent_id, a.customer_id, a.status FROM agents a JOIN customers c "
                     "ON c.customer_id = a.customer_id WHERE c.name = :c AND a.name = :a "
                     "ORDER BY a.created_at LIMIT 1", c=PUBLIC_CUSTOMER, a=AGENTS[slug]).mappings().first()
    if row is None:
        raise DemoUnavailable("public demo agents are not seeded")
    return dict(row)


def public_agent_ids(conn: Any) -> set[str]:
    rows = db.q(conn, "SELECT a.agent_id FROM agents a JOIN customers c ON c.customer_id = "
                      "a.customer_id WHERE c.name = :c", c=PUBLIC_CUSTOMER).all()
    return {r[0] for r in rows}


def check_rate(ip: str) -> None:
    """At most RUNS_PER_WINDOW runs per WINDOW_SECONDS per visitor IP (shared across containers)."""
    since = timeutil.to_db(timeutil.now() - timedelta(seconds=WINDOW_SECONDS))
    with db.connection() as conn:
        rows = db.q(conn, "SELECT at FROM events WHERE type = 'demo.run' AND at >= :s AND "
                          "JSON_UNQUOTE(JSON_EXTRACT(body_json, '$.ip')) = :h AND "
                          "JSON_EXTRACT(body_json, '$.script') IS NULL ORDER BY at",
                    s=since, h=ip_hash(ip)).all()
    if len(rows) >= RUNS_PER_WINDOW:
        oldest = timeutil.from_db(rows[0][0])
        wait = WINDOW_SECONDS - int((timeutil.now() - oldest).total_seconds())
        raise RateLimited(max(wait, 1))


# ---------------------------------------------------------------- run recording


class _Run:
    def __init__(self, scenario: str, ip: str, trace_id: str | None = None,
                 extra: dict[str, Any] | None = None) -> None:
        self.scenario = scenario
        if trace_id:  # resume an existing session (playground)
            self.trace_id = trace_id
            return
        self.trace_id = new_id("trc")
        with db.transaction() as conn:
            events.append(conn, trace_id=self.trace_id, type="demo.run", actor_type="gateway",
                          audience="aimo_only",
                          body={"scenario": scenario, "ip": ip_hash(ip), **(extra or {})},
                          text=f"Public demo run started: {scenario.replace('_', ' ')}.")

    def step(self, title: str, summary: str, status: str = "ok",
             detail: Any = None, request: Any = None, kind: str | None = None,
             extra: dict[str, Any] | None = None) -> None:
        """Record one step: what was sent (`request`) and what came back (`detail`).
        `kind` and `extra` are used by the live demo (live_demo.py)."""
        body = {"title": title, "summary": summary, "status": status, "detail": detail,
                "request": request}
        if kind:
            body["kind"] = kind
        body.update(extra or {})
        with db.transaction() as conn:
            events.append(conn, trace_id=self.trace_id, type="demo.step", actor_type="gateway",
                          audience="aimo_only", text=f"Demo step: {title}"[:500], body=body)


def earliest_delivery_date(product_ids: list[str], trace_id: str,
                           agent_id: str) -> date | None:
    """Earliest date upstream can deliver any shortlisted product with a cut-off still ahead
    (document 09, section 6a). Upstream answers with its own earliest date per product."""
    facts = upstream.get_adapter().get_facts(product_ids, timeutil.today_helsinki(),
                                             trace_id=trace_id, actor_id=agent_id)
    soon = timeutil.now() + timedelta(minutes=5)
    dates = []
    for f in facts.values():
        av = f.availability
        if av is None or not av.has_schedule or av.sales_block or av.available_date is None:
            continue
        if av.available_amount is not None and av.available_amount <= 0:
            continue
        if av.closing_time is not None and av.closing_time <= soon:
            continue
        dates.append(av.available_date)
    return max(min(dates), timeutil.today_helsinki()) if dates else None


def _excluded_summary(excluded: list[dict[str, Any]]) -> list[str]:
    out = []
    for x in excluded[:3]:
        msg = (x.get("policy_reason") or {}).get("message") or x.get("message") or ""
        out.append(f"{x.get('product_id')}: {msg}")
    if len(excluded) > 3:
        out.append(f"...and {len(excluded) - 3} more for the same kind of reason.")
    return out


# ---------------------------------------------------------------- scenarios


def run(scenario: str, ip: str) -> str:
    """Run one scenario for a visitor. Returns the trace ID. Raises RateLimited."""
    if scenario not in SCENARIOS:
        raise ValueError(scenario)
    check_rate(ip)
    r = _Run(scenario, ip)
    try:
        if scenario == "anonymous_search":
            _anonymous_search(r)
        else:
            _offer_flow(r, "order" if scenario == "example_order" else "allergy")
    except HandshakeError as e:
        r.step("The gateway answered with an error", f"{e.code}: {e.detail}", "bad",
               {"code": e.code, "detail": e.detail, "next": e.next})
    return r.trace_id


SEARCH_REQUEST = {"category": "10400", "claims": ["FREE_FROM_LACTOSE"],
                  "name_excludes": PLAIN_MILK_EXCLUDES, "limit": 20}


def _search(conn: Any, limit: int = 20) -> list[dict[str, Any]]:
    res = catalog.search(conn, category="10400", claims=["FREE_FROM_LACTOSE"],
                         name_excludes=PLAIN_MILK_EXCLUDES, limit=limit)
    return [i.dump() for i in res.items]


def _anonymous_search(r: _Run) -> None:
    with db.connection() as conn:
        items = _search(conn)
    sample = [{k: i[k] for k in ("product_id", "name", "claims", "allergens", "comparison")}
              for i in items[:3]]
    text = repr(items).lower()
    leaked = any(w in text for w in ("price", "availab", "stock_"))
    r.step("Search without an agent key",
           f"GET /v1/products?category=10400&claims=FREE_FROM_LACTOSE returned {len(items)} "
           f"lactose-free milks with structured claims and allergens.", "ok", sample,
           request=SEARCH_REQUEST | {"authorization": None})
    r.step("No prices, no stock for anonymous callers",
           "The answer has no price or availability field: those come only from a signed offer "
           "to an authenticated agent." if not leaked else
           "Unexpected: a price or stock field was present.", "ok" if not leaked else "bad")


def _offer_flow(r: _Run, slug: str) -> None:
    with db.connection() as conn:
        agent = _agent(conn, slug)
        mandate = mandates.get_current(conn, agent["agent_id"])
        items = _search(conn)
    m = mandate.public() if mandate else {}
    fa = (mandate.forbidden_allergens.codes if mandate else []) or []
    r.step("The agent reads its mandate",
           f"Cap {m.get('limits', {}).get('max_order_total_excl_tax')} EUR per order, excl. tax."
           + (f" Forbidden allergens: {', '.join(fa)} (contains or may contain; unknown is "
              f"rejected)." if fa else " No forbidden allergens.")
           + " The gateway, not the agent, enforces it.", "ok", m,
           request={"tool": "get_mandate", "http": "GET /v1/mandates/current"})
    found = {i["product_id"] for i in items}
    shortlist = [p for p in SHORTLIST if p in found] or [i["product_id"] for i in items[:8]]
    r.step("The agent searches the catalogue",
           f"Category 10400 (milk), claim FREE_FROM_LACTOSE, coffee and sour milks excluded: "
           f"{len(items)} products. Shortlist in order of preference: {', '.join(shortlist)}.",
           "ok", [{"product_id": i["product_id"], "name": i["name"].get("en"),
                   "allergens": i["allergens"]} for i in items[:8]],
           request={"tool": "search_products", "http": "GET /v1/products", **SEARCH_REQUEST})
    delivery = earliest_delivery_date(shortlist, r.trace_id, agent["agent_id"])
    if delivery is None:
        r.step("No delivery date available",
               "Upstream reports no deliverable date with an open order cut-off right now. "
               "Try again later.", "bad")
        return
    r.step("Earliest delivery date from Valio Aimo",
           f"{delivery.isoformat()}: the earliest date upstream can deliver with the order "
           f"cut-off still ahead.", "ok")
    req = OfferRequest.model_validate({
        "delivery_date": delivery.isoformat(), "max_options": 1,
        "lines": [{"product_ids": shortlist, "quantity": {"value": 1000, "unit": "L"},
                   "requirements": {"required_claims": ["FREE_FROM_LACTOSE"]},
                   "fulfilment": "allow_split", "substitution": "none"}]})
    try:
        body = offers.build_offer(req, agent_id=agent["agent_id"],
                                  customer_id=agent["customer_id"], trace_id=r.trace_id)
    except HandshakeError as e:
        if e.code != "no_candidates":
            raise
        excluded = e.extra.get("excluded") or []
        r.step("Refused by the gateway: no product can be offered under this mandate",
               " ".join(_excluded_summary(excluded)), "refused",
               {"code": e.code, "excluded": excluded[:5]},
               request={"tool": "request_offer", "http": "POST /v1/offers", **req.dump()})
        return
    option = body["options"][0]
    offer, jws = option["offer"], option["jws"]
    payload = signing.verify(jws, signing.jwks(), signing.TYP_OFFER)  # as any agent would
    lines = "; ".join(f"{ln['quantity']['value']} x {ln['product_id']} ({ln['role']}, stock "
                      f"{ln['availability']['assurance']})" for ln in offer["lines"])
    r.step("Signed, fixed-price offer",
           f"{lines}. Total {offer['totals']['excl_tax']} EUR excl. tax, valid until "
           f"{timeutil.iso(timeutil.parse_upstream(offer['expires_at']))[11:16]}. Signature "
           f"verified with the published key {get_settings().signing_kid} (EdDSA).", "ok",
           {"offer": payload, "jws": jws},
           request={"tool": "request_offer", "http": "POST /v1/offers", **req.dump()})
    dry, _ = orders.dry_run(offer["offer_id"], agent["agent_id"], agent["status"])
    reasons = " ".join(x["message"] for x in dry["reasons"]) or "No rule fired."
    r.step(f"Dry run against the mandate: {dry['outcome'].replace('_', ' ')}", reasons,
           "warn" if dry["outcome"] == "needs_approval" else
           ("ok" if dry["outcome"] == "accepted" else "bad"), dry,
           request={"tool": "dry_run_order", "http": "POST /v1/orders/dry-run",
                    "offer_id": offer["offer_id"]})
    status, order = orders.place_order(
        OrderRequest(offer_id=offer["offer_id"], reference="PUBLIC-DEMO"),
        agent_id=agent["agent_id"], customer_id=agent["customer_id"],
        agent_status=agent["status"], idem_key=f"demo-{r.trace_id}",
        approval_ttl_seconds=APPROVAL_TTL_SECONDS)
    r.step(f"Order placed: {order['status'].replace('_', ' ')}",
           f"Order {order['order_id']}. "
           + ("It waits for a human: you are the approver in this demo. The agent never sees "
              "the approval link." if order["status"] == "pending_approval" else ""),
           "warn" if order["status"] == "pending_approval" else "ok",
           {k: v for k, v in order.items() if k != "links"},
           request={"tool": "place_order", "http": "POST /v1/orders",
                    "offer_id": offer["offer_id"], "reference": "PUBLIC-DEMO"})


# ---------------------------------------------------------------- run view


def run_view(trace_id: str) -> dict[str, Any] | None:
    """Steps and the order of one public-demo run. None if the trace is not a public demo run."""
    with db.connection() as conn:
        evs = events.list_events(conn, trace_id)
    if not evs or evs[0]["type"] != "demo.run":
        return None
    view: dict[str, Any] = {
        "trace_id": trace_id, "scenario": evs[0]["body"].get("scenario"),
        "sandbox_id": evs[0]["body"].get("sandbox"), "persona": evs[0]["body"].get("persona"),
        "script": evs[0]["body"].get("script"),
        "started_at": evs[0]["at"],
        "steps": [e["body"] | {"at": e["at"]} for e in evs if e["type"] == "demo.step"],
        "order": None,
    }
    with db.transaction() as conn:
        public = public_agent_ids(conn)
        # A scenario may place several orders in one trace: show the pending one, else the
        # latest.
        row = db.q(conn, "SELECT o.order_id, o.agent_id, c.sandbox_id FROM orders o "
                         "JOIN agents a ON a.agent_id = o.agent_id JOIN customers c ON "
                         "c.customer_id = a.customer_id WHERE o.trace_id = :t ORDER BY "
                         "o.status = 'pending_approval' DESC, o.created_at DESC, o.order_id",
                   t=trace_id).first()
        # Only the public demo agents' orders, or an order of this run's own sandbox; never
        # any other order.
        ok = row is not None and (row[1] in public or (
            row[2] is not None and row[2] == view["sandbox_id"]))
        if ok:
            orders.expire_approval_if_due(conn, row[0])
    if ok:
        with db.connection() as conn:
            o = orders.load_order(conn, row[0])
            appr = orders.load_approval(conn, row[0])
        info: dict[str, Any] = {"order_id": o["order_id"], "status": o["status"],
                                "receipt_url": receipts.human_url(o)}
        if appr is not None:
            info["approval_status"] = appr["status"]
            info["approval_expires_at"] = appr["expires_at"]  # aware (orders._approval_row)
            if appr["status"] == "pending":
                info["approval_url"] = orders.approval_url(appr["approval_id"])
        view["order"] = info
    return view


# ---------------------------------------------------------------- playground (one tool per card)

PLAYGROUND_STEPS = ("search", "request_offer", "verify_signature", "dry_run", "place_order",
                    "get_receipt")
TOOL_NAMES = {"search": "search_products", "request_offer": "request_offer",
              "verify_signature": "verify_offer (signature check)", "dry_run": "dry_run_order",
              "place_order": "place_order", "get_receipt": "get_receipt"}
MAX_SEARCHES_PER_SESSION = 5


class PlaygroundError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _session_offer(conn: Any, trace_id: str, agent_id: str) -> dict[str, Any] | None:
    row = db.q(conn, "SELECT offer_id FROM offers WHERE trace_id = :t AND agent_id = :a "
                     "ORDER BY issued_at LIMIT 1", t=trace_id, a=agent_id).first()
    return offers.load_offer(conn, row[0]) if row else None


def _session_order(conn: Any, trace_id: str, agent_id: str) -> dict[str, Any] | None:
    row = db.q(conn, "SELECT order_id FROM orders WHERE trace_id = :t AND agent_id = :a",
               t=trace_id, a=agent_id).first()
    return orders.load_order(conn, row[0]) if row else None


def playground(step: str, ip: str, trace_id: str | None = None,
               token: str | None = None) -> dict[str, Any]:
    """Perform one example-order step as the public-demo agent in the visitor's session.

    A session is a trace. Starting one counts as one demo run against the visitor limit; the
    steps inside it do not count again, but each write step runs at most once per session and
    searches are capped, so a session's work is bounded. Returns the tool name, the request the
    agent sends, the real response, and (place_order only) the visitor's own approval link.
    """
    if step not in PLAYGROUND_STEPS:
        raise PlaygroundError(400, "unknown_step", "Unknown playground step.")
    if trace_id:
        if not check_run_token(trace_id, token):
            raise PlaygroundError(404, "unknown_session", "This playground session is not valid.")
        with db.connection() as conn:
            first = db.q(conn, "SELECT type, body_json FROM events WHERE trace_id = :t AND "
                               "seq = 1", t=trace_id).first()
        if (not first or first[0] != "demo.run"
                or db.jload(first[1]).get("scenario") != "playground"):
            raise PlaygroundError(404, "unknown_session", "This playground session is not valid.")
        r = _Run("playground", ip, trace_id=trace_id)
    else:
        if step not in ("search", "request_offer"):  # nothing to act on yet; spend no run
            raise PlaygroundError(409, "needs_request_offer", "Run the request offer step first.")
        check_rate(ip)  # a new session is one demo run
        r = _Run("playground", ip)
    with db.connection() as conn:
        agent = _agent(conn, "order")
        offer_row = _session_offer(conn, r.trace_id, agent["agent_id"])
        order_row = _session_order(conn, r.trace_id, agent["agent_id"])
        searches = int(db.q(conn, "SELECT COUNT(*) FROM events WHERE trace_id = :t AND type = "
                                  "'demo.step' AND text = 'Demo step: playground search'",
                            t=r.trace_id).scalar_one())
    out: dict[str, Any] = {"session": {"trace_id": r.trace_id, "k": run_token(r.trace_id)},
                           "step": step, "tool": TOOL_NAMES[step]}

    def need(row: Any, what: str) -> None:
        if row is None:
            raise PlaygroundError(409, f"needs_{what}",
                                  f"Run the {what.replace('_', ' ')} step first.")

    try:
        if step == "search":
            if searches >= MAX_SEARCHES_PER_SESSION:
                raise PlaygroundError(429, "too_many_searches",
                                      "This session has used its searches. Reload the page "
                                      "to start a new session.")
            req = SEARCH_REQUEST | {"limit": 5}
            with db.connection() as conn:
                res = catalog.search(conn, category="10400", claims=["FREE_FROM_LACTOSE"],
                                     name_excludes=PLAIN_MILK_EXCLUDES, limit=5)
            out.update(request={"http": "GET /v1/products", **req}, response=res.dump())
        elif step == "request_offer":
            if offer_row is not None:  # one offer per session; show the one already issued
                out.update(request={"http": "POST /v1/offers", **offer_row["request"]},
                           response={"trace_id": r.trace_id, "options": [
                               {"label": "best", "offer": offer_row["payload"],
                                "jws": offer_row["jws"]}]},
                           note="This session already has an offer; it is shown again.")
            else:
                delivery = earliest_delivery_date(SHORTLIST, r.trace_id, agent["agent_id"])
                if delivery is None:
                    raise PlaygroundError(503, "no_delivery_date", "Upstream reports no "
                                          "deliverable date with an open cut-off right now.")
                req = OfferRequest.model_validate({
                    "delivery_date": delivery.isoformat(), "max_options": 1,
                    "lines": [{"product_ids": SHORTLIST,
                               "quantity": {"value": 1000, "unit": "L"},
                               "requirements": {"required_claims": ["FREE_FROM_LACTOSE"]},
                               "fulfilment": "allow_split", "substitution": "none"}]})
                body = offers.build_offer(req, agent_id=agent["agent_id"],
                                          customer_id=agent["customer_id"], trace_id=r.trace_id)
                out.update(request={"http": "POST /v1/offers", **req.dump()}, response=body)
        elif step == "verify_signature":
            need(offer_row, "request_offer")
            import jwt as _jwt

            jws = offer_row["jws"]
            header = _jwt.get_unverified_header(jws)
            try:
                payload = signing.verify(jws, signing.jwks(), signing.TYP_OFFER)
                verified = True
            except signing.SignatureError:
                payload, verified = None, False
            out.update(request={"jws": jws, "jwks_uri": f"{get_settings().public_base_url}"
                                                        f"/.well-known/jwks.json"},
                       response={"verified": verified, "header": header,
                                 "offer_id": (payload or {}).get("offer_id"),
                                 "totals": (payload or {}).get("totals"),
                                 "expires_at": (payload or {}).get("expires_at")})
        elif step == "dry_run":
            need(offer_row, "request_offer")
            dry, _ = orders.dry_run(offer_row["offer_id"], agent["agent_id"], agent["status"])
            out.update(request={"http": "POST /v1/orders/dry-run",
                                "offer_id": offer_row["offer_id"]}, response=dry)
        elif step == "place_order":
            need(offer_row, "request_offer")
            req_o = OrderRequest(offer_id=offer_row["offer_id"], reference="PUBLIC-PLAYGROUND")
            _, order = orders.place_order(
                req_o, agent_id=agent["agent_id"], customer_id=agent["customer_id"],
                agent_status=agent["status"], idem_key=f"pg-{r.trace_id}",
                approval_ttl_seconds=APPROVAL_TTL_SECONDS)
            out.update(request={"http": "POST /v1/orders",
                                "headers": {"Idempotency-Key": f"pg-{r.trace_id}"},
                                **req_o.dump(exclude_none=True)},
                       response=order)
            with db.connection() as conn:
                appr = orders.load_approval(conn, order["order_id"])
            if appr is not None and appr["status"] == "pending":
                # The visitor is the approver in this demo: their own order's link only.
                out["you"] = {"approval_url": orders.approval_url(appr["approval_id"]),
                              "approval_expires_at": timeutil.iso(appr["expires_at"])}
        elif step == "get_receipt":
            need(order_row, "place_order")
            out.update(request={"http": f"GET /v1/orders/{order_row['order_id']}/receipt"},
                       response=receipts.get_receipt(order_row["order_id"], agent["agent_id"]))
    except HandshakeError as e:
        out.update(response={"error": e.problem(r.trace_id)})
    r.step(f"playground {step.replace('_', ' ')}", f"Playground: {TOOL_NAMES[step]}",
           "bad" if "error" in (out.get("response") or {}) else "ok",
           request=out.get("request"))
    return out
