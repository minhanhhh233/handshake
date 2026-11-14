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
                          "JSON_UNQUOTE(JSON_EXTRACT(body_json, '$.ip')) = :h ORDER BY at",
                    s=since, h=ip_hash(ip)).all()
    if len(rows) >= RUNS_PER_WINDOW:
        oldest = timeutil.from_db(rows[0][0])
        wait = WINDOW_SECONDS - int((timeutil.now() - oldest).total_seconds())
        raise RateLimited(max(wait, 1))


# ---------------------------------------------------------------- run recording


class _Run:
    def __init__(self, scenario: str, ip: str) -> None:
        self.trace_id = new_id("trc")
        self.scenario = scenario
        with db.transaction() as conn:
            events.append(conn, trace_id=self.trace_id, type="demo.run", actor_type="gateway",
                          audience="aimo_only", body={"scenario": scenario, "ip": ip_hash(ip)},
                          text=f"Public demo run started: {scenario.replace('_', ' ')}.")

    def step(self, title: str, summary: str, status: str = "ok",
             detail: Any = None) -> None:
        with db.transaction() as conn:
            events.append(conn, trace_id=self.trace_id, type="demo.step", actor_type="gateway",
                          audience="aimo_only", text=f"Demo step: {title}"[:500],
                          body={"title": title, "summary": summary, "status": status,
                                "detail": detail})


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


def _search(conn: Any) -> list[dict[str, Any]]:
    res = catalog.search(conn, category="10400", claims=["FREE_FROM_LACTOSE"],
                         name_excludes=PLAIN_MILK_EXCLUDES, limit=20)
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
           f"lactose-free milks with structured claims and allergens.", "ok", sample)
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
           + " The gateway, not the agent, enforces it.", "ok", m)
    found = {i["product_id"] for i in items}
    shortlist = [p for p in SHORTLIST if p in found] or [i["product_id"] for i in items[:8]]
    r.step("The agent searches the catalogue",
           f"Category 10400 (milk), claim FREE_FROM_LACTOSE, coffee and sour milks excluded: "
           f"{len(items)} products. Shortlist in order of preference: {', '.join(shortlist)}.",
           "ok", [{"product_id": i["product_id"], "name": i["name"].get("en"),
                   "allergens": i["allergens"]} for i in items[:8]])
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
               {"code": e.code, "excluded": excluded[:5]})
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
           {"offer": payload, "jws": jws})
    dry, _ = orders.dry_run(offer["offer_id"], agent["agent_id"], agent["status"])
    reasons = " ".join(x["message"] for x in dry["reasons"]) or "No rule fired."
    r.step(f"Dry run against the mandate: {dry['outcome'].replace('_', ' ')}", reasons,
           "warn" if dry["outcome"] == "needs_approval" else
           ("ok" if dry["outcome"] == "accepted" else "bad"), dry)
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
           {k: v for k, v in order.items() if k != "links"})


# ---------------------------------------------------------------- run view


def run_view(trace_id: str) -> dict[str, Any] | None:
    """Steps and the order of one public-demo run. None if the trace is not a public demo run."""
    with db.connection() as conn:
        evs = events.list_events(conn, trace_id)
    if not evs or evs[0]["type"] != "demo.run":
        return None
    view: dict[str, Any] = {
        "trace_id": trace_id, "scenario": evs[0]["body"].get("scenario"),
        "started_at": evs[0]["at"],
        "steps": [e["body"] | {"at": e["at"]} for e in evs if e["type"] == "demo.step"],
        "order": None,
    }
    with db.transaction() as conn:
        public = public_agent_ids(conn)
        row = db.q(conn, "SELECT order_id, agent_id FROM orders WHERE trace_id = :t",
                   t=trace_id).first()
        if row and row[1] in public:  # only the public demo agents' orders, never any other
            orders.expire_approval_if_due(conn, row[0])
    if row and row[1] in public:
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
