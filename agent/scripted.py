"""Scripted buyer agent (task T-304; document 07, section 5; document 09, sections 4 and 6).

Replays the call sequence of document 07, section 4.4 with fixed parameters and no language
model. It is the end-to-end test and the live fallback for the Claude agent, so it prints the
same kind of console output: each tool call and a compact result.

    python agent/scripted.py --scenario milk_order
    python agent/scripted.py --scenario milk_split --delivery-date 2026-11-16
    python agent/scripted.py --scenario milk_order --base-url http://127.0.0.1:8000 \
        --agent nordlys --poll-timeout 120 --json

Scenarios:
  milk_order  1,000 L of lactose-free milk, 464306 first, then the rest of the shortlist.
  milk_split  1,000 L with 469363 first, then 464306 (substitution allowed): 384 + 616 units.
  allergy_guard  the milk_order request under a milk-allergy mandate (default agent koivula).
              The gateway MUST refuse with 422 no_candidates; that refusal is the success
              (exit 0, final_status "refused"). An issued offer is a failure (exit 1); the
              agent never retries with other products and never dry-runs or orders.

Like any third-party agent it uses only `handshake_client` (never the gateway package). It never
approves anything and never asks for an approval link (FR-35): a human approves out of band.

Key: --key, else --agent NAME from keys/demo_agents.json when given, else HANDSHAKE_AGENT_KEY,
else the "nordlys" entry of keys/demo_agents.json; for allergy_guard without --key or --agent
the agent is "koivula". Base URL: --base-url, else
HANDSHAKE_BASE_URL, else http://127.0.0.1:8000.

Exit codes: 0 the order reached a final status (allergy_guard: the expected refusal); 1 an error (gateway error, signature failure,
bad arguments); 3 still pending approval when --poll-timeout passed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx

try:  # imported as part of the `agent` package
    from .handshake_client import HandshakeAPIError, HandshakeClient, SignatureVerificationError
except ImportError:  # run as a script: python agent/scripted.py
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from handshake_client import (  # type: ignore[no-redef]
        HandshakeAPIError,
        HandshakeClient,
        SignatureVerificationError,
    )

ROOT = Path(__file__).resolve().parent.parent
KEYS_FILE = ROOT / "keys" / "demo_agents.json"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
HELSINKI = ZoneInfo("Europe/Helsinki")
MIN_POLL_INTERVAL = 5.0  # document 07, sections 4.2 and 7: never poll faster than every 5 s
MAX_PRODUCT_IDS = 8  # document 07, section 4.4: up to 8 product IDs

# Document 06, section 4.1: plain lactose-free milk.
MILK_CATEGORY = "10400"
MILK_CLAIMS = ["FREE_FROM_LACTOSE"]
NAME_EXCLUDES = ["sour", "piimä", "coffee", "kahvi", "latte", "portion", "annos", "café", "cafe"]
# Document 06, section 4.2: the rest of the shortlist (plain 1 l / 10 l), in preference order.
SHORTLIST_PREFERENCE = ["464398", "464201", "465039", "469363", "462775", "464179", "463038"]

SCENARIOS: dict[str, dict[str, Any]] = {
    "milk_order": {
        "title": "1,000 L of lactose-free milk by Monday",
        "lead": ["464306"],
        "fill_from_search": True,
        "reference_suffix": "milk",
    },
    "milk_split": {
        "title": "1,000 L of lactose-free milk, 469363 first (split on real stock)",
        "lead": ["469363", "464306"],
        "fill_from_search": False,
        "reference_suffix": "milk-split",
    },
    # Task T-405 (FR-37; document 09, sections 3 API-21, 4 and 6 step 9): the milk-allergy
    # mandate asks for the same lactose-free milk as milk_order. The gateway MUST refuse.
    "allergy_guard": {
        "title": "lactose-free milk under a milk-allergy mandate (expect a refusal)",
        "lead": ["464306"],
        "fill_from_search": True,
        "reference_suffix": "allergy",
        "expect_refusal": True,
        "default_agent": "koivula",
    },
}
FINAL_STATUSES = {"confirmed", "declined", "expired", "rejected"}
REFUSED = "refused"  # allergy_guard: the expected 422 no_candidates (a success for that scenario)
ALLERGEN_NAMES = {"AM": "milk"}
STATE_WORDS = {"CONTAINS": "contains", "MAY_CONTAIN": "may contain"}
UNKNOWN_WORDS = {
    "reject": "unknown status is rejected",
    "needs_approval": "unknown status needs approval",
    "allow": "unknown status is allowed",
}

Out = Callable[..., Any]


# ---------------------------------------------------------------- helpers


def next_monday(today: date | None = None) -> date:
    """The next Monday strictly after today, in Europe/Helsinki ("by Monday")."""
    today = today or datetime.now(HELSINKI).date()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def _num(v: Any) -> str:
    """Compact number: 1000.0 -> '1000', '384.000' -> '384'."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _name(obj: dict[str, Any]) -> str:
    n = obj.get("name")
    if isinstance(n, dict):
        n = n.get("en") or n.get("fi") or next((x for x in n.values() if x), "")
    return str(n or "")


def _short(text: str, width: int = 60) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _local(ts: Any) -> str:
    """ISO timestamp -> 'YYYY-MM-DD HH:MM' in Helsinki; unparsable values unchanged."""
    if not isinstance(ts, str):
        return str(ts)
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    if dt.tzinfo is None:
        return ts
    return dt.astimezone(HELSINKI).strftime("%Y-%m-%d %H:%M")


def _countdown(ts: Any) -> str:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        secs = int((dt - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError):
        return ""
    if secs <= 0:
        return "expired"
    return f"in {secs // 60} min {secs % 60:02d} s"


def _dot(out: Out) -> None:
    try:
        out(".", end="", flush=True)
    except TypeError:  # an `out` that is not print-like
        out(".")


def _reasons(reasons: list[dict[str, Any]] | None) -> list[str]:
    return [f"{r.get('code')}: {r.get('message')}" for r in reasons or []]


class ScriptStop(Exception):
    """An expected stop: printed as a readable message, never as a stack trace."""

    def __init__(self, kind: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.details = details or {}


# ---------------------------------------------------------------- printing


def _print_search(out: Out, res: dict[str, Any]) -> None:
    items = res.get("items") or []
    out(f"  -> {res.get('count', len(items))} products: "
        + ", ".join(i.get("product_id", "?") for i in items))
    for i in items:
        if i.get("product_id") in {"464306", *SHORTLIST_PREFERENCE}:
            cat = (i.get("category") or {}).get("code")
            claims = i.get("claims") or []
            shown = [c for c in claims if c in MILK_CLAIMS]
            more = f" +{len(claims) - len(shown)} more" if len(claims) > len(shown) else ""
            out(f"     {i['product_id']}  {_short(_name(i), 58)}  [cat {cat}; "
                f"{', '.join(shown) or 'no required claim'}{more}]")


def _print_offer(out: Out, data: dict[str, Any]) -> dict[str, Any]:
    options = data.get("options") or []
    excluded = data.get("excluded") or []
    if not options:
        raise ScriptStop("no_offer", "The gateway returned no offer option.")
    option = options[0]
    offer = option["offer"]
    out(f"  -> offer {offer['offer_id']} ({option.get('label', 'best')}), delivery "
        f"{offer.get('delivery_date')}, mandate v{offer.get('mandate_version')}")
    for ln in offer.get("lines") or []:
        q = ln.get("quantity") or {}
        bq = ln.get("base_quantity") or {}
        av = ln.get("availability") or {}
        role = ln.get("role")
        sub = f" for {ln['substitute_for']}" if ln.get("substitute_for") else ""
        base = f" (= {_num(bq.get('value'))} {bq.get('unit')})" if bq else ""
        out(f"     line {ln.get('line_no')}: {_num(q.get('value'))} {ln.get('sales_unit')}{base} "
            f"x {ln.get('product_id')} [{role}{sub}] @ {ln.get('unit_price_excl_tax')} = "
            f"{ln.get('line_total_excl_tax')} EUR excl. tax")
        out(f"             {_short(ln.get('name') or '', 64)}")
        order_by = f", order by {_local(av.get('order_by'))}" if av.get("order_by") else ""
        out(f"             stock {av.get('assurance')} ({av.get('logistic_model')}), available "
            f"{av.get('available_date')}{order_by}")
    for f in offer.get("fulfilment") or []:
        req, got = f.get("requested") or {}, f.get("offered") or {}
        state = "complete" if f.get("complete") else "INCOMPLETE"
        out(f"     fulfilment line {f.get('request_line')}: requested {_num(req.get('value'))} "
            f"{req.get('unit')}, offered {_num(got.get('value'))} {got.get('unit')} ({state}"
            + (", split" if f.get("split") else "") + ")")
    t = offer.get("totals") or {}
    out(f"     totals: {t.get('excl_tax')} EUR excl. tax, tax {t.get('tax')}, "
        f"{t.get('incl_tax')} EUR incl. tax")
    out(f"     expires {_local(offer.get('expires_at'))} ({_countdown(offer.get('expires_at'))})")
    sig = option.get("signature") or {}
    if sig.get("verified"):
        out(f"     signature verified (kid {sig.get('kid')})")
    if excluded:
        out(f"     excluded {len(excluded)}: " + "; ".join(
            f"{e.get('product_id')} {e.get('reason')}" for e in excluded))
    return option


def _forbidden_text(mandate: dict[str, Any]) -> str:
    """Plain words for the mandate's forbidden allergens, e.g. 'This mandate forbids milk (AM):
    contains or may contain; unknown status is rejected.'"""
    fa = mandate.get("forbidden_allergens") or {}
    codes = fa.get("codes") or []
    if not codes:
        return "This mandate forbids no allergens."
    what = ", ".join(f"{ALLERGEN_NAMES.get(c, c)} ({c})" if c in ALLERGEN_NAMES else c
                     for c in codes)
    states = " or ".join(STATE_WORDS.get(s, s) for s in fa.get("states") or [])
    unknown = UNKNOWN_WORDS.get(fa.get("unknown"), f"unknown status: {fa.get('unknown')}")
    return f"This mandate forbids {what}: {states or 'any state'}; {unknown}."


def _print_allergens(out: Out, res: dict[str, Any], codes: list[str]) -> None:
    """For each search result, the state of the mandate's forbidden allergens (default AM)."""
    items = res.get("items") or []
    codes = codes or ["AM"]
    states_seen: list[str] = []
    for i in items:
        stmts = {a.get("code"): a.get("state") for a in i.get("allergens") or []}
        shown = ", ".join(f"{c} {stmts.get(c) or 'UNKNOWN'}" for c in codes)
        states_seen.append(stmts.get(codes[0]) or "UNKNOWN")
        out(f"     {i.get('product_id')}  {_short(_name(i), 52)}  [{shown}]")
    if items and all(s == "CONTAINS" for s in states_seen):
        out(f"  All {len(items)} results are lactose-free AND carry {codes[0]} CONTAINS. "
            "Lactose-free is not milk-free.")


def _print_order(out: Out, order: dict[str, Any]) -> None:
    out(f"  -> order {order.get('order_id')}: status {order.get('status')}, outcome "
        f"{order.get('outcome')}")
    for r in _reasons(order.get("reasons")):
        out(f"     reason {r}")


def _refusal(out: Out, summary: dict[str, Any], e: HandshakeAPIError) -> dict[str, Any]:
    """allergy_guard: print the gateway's refusal and fill the summary. Never works around it.

    Success only when every candidate was excluded forbidden_by_mandate; a 422 for any other
    reason (dates, prices) is not the refusal this scenario demonstrates.
    """
    excluded = (e.problem or {}).get("excluded") or []
    out(f"  -> gateway answered {e.status} {e.code}")
    out("")
    out("Refused by the gateway: no candidate can be offered under this mandate.")
    for x in excluded:
        pr = x.get("policy_reason") or {}
        msg = pr.get("message") or x.get("message") or ""
        rule = f" [{pr.get('rule')}]" if pr.get("rule") else ""
        out(f"  {x.get('product_id')}  {x.get('reason')}{rule}: {msg}")
    summary["final_status"] = REFUSED
    summary["outcome"] = "rejected"
    summary["code"] = e.code
    summary["excluded"] = [
        {"product_id": x.get("product_id"), "reason": x.get("reason"),
         "policy_reason": (x.get("policy_reason") or {}).get("code"),
         "message": (x.get("policy_reason") or {}).get("message") or x.get("message")}
        for x in excluded
    ]
    if not excluded or any(x.get("reason") != "forbidden_by_mandate" for x in excluded):
        other = sorted({str(x.get("reason")) for x in excluded} - {"forbidden_by_mandate"})
        out("FAIL: the refusal is not (only) a mandate refusal"
            + (f": also {', '.join(other)}" if other else ": no excluded products listed")
            + ".")
        summary["error"] = {"type": "unexpected_refusal", "status": e.status, "code": e.code,
                            "message": "not every candidate was excluded forbidden_by_mandate"}
        return summary
    out("Result: refused as expected; no dry-run, no order.")
    return summary


# ---------------------------------------------------------------- the scenario


def run(
    scenario: str,
    client: HandshakeClient,
    delivery_date: date | str,
    poll_timeout: float,
    poll_interval: float = MIN_POLL_INTERVAL,
    out: Out = print,
) -> dict[str, Any]:
    """Run one scenario with `client`. Returns the summary (also printed with --json).

    Expected failures (gateway errors, signature failures) are printed as readable messages
    and returned in `summary["error"]`; they never raise.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; choose from {', '.join(SCENARIOS)}")
    spec = SCENARIOS[scenario]
    delivery = delivery_date.isoformat() if isinstance(delivery_date, date) else str(delivery_date)
    summary: dict[str, Any] = {
        "scenario": scenario, "trace_id": None, "delivery_date": delivery, "offer_id": None,
        "order_id": None, "final_status": None, "outcome": None, "dry_run_outcome": None,
        "lines": [], "error": None,
    }
    step = 0

    def call(text: str) -> None:
        nonlocal step
        step += 1
        out(f"[{step}] {text}")

    try:
        trace_id = client.new_task()
        summary["trace_id"] = trace_id
        out(f"Scenario {scenario}: {spec['title']}; delivery {delivery}")
        out(f"Trace {trace_id}")

        disc = client.discovery()
        out(f"[0] discovery: api_base {disc.get('api_base')}, keys {disc.get('jwks_uri')}")

        call("get_mandate()")
        mandate = client.get_mandate()
        limits = mandate.get("limits") or {}
        approvers = ", ".join(a.get("name", "?") for a in mandate.get("approvers") or [])
        out(f"  -> mandate {mandate.get('mandate_id')} v{mandate.get('version')}: cap "
            f"{limits.get('max_order_total_excl_tax')} {limits.get('currency', 'EUR')} per order "
            f"excl. tax" + (f"; approver {approvers}" if approvers else ""))
        expect_refusal = bool(spec.get("expect_refusal"))
        forbidden_codes = list((mandate.get("forbidden_allergens") or {}).get("codes") or [])
        if expect_refusal or forbidden_codes:
            out(f"     {_forbidden_text(mandate)}")

        call(f"search_products(category={MILK_CATEGORY!r}, claims={MILK_CLAIMS}, "
             f"name_excludes={NAME_EXCLUDES})")
        found = client.search_products(category=MILK_CATEGORY, claims=MILK_CLAIMS,
                                       name_excludes=NAME_EXCLUDES)
        if expect_refusal:
            items = found.get("items") or []
            out(f"  -> {found.get('count', len(items))} products:")
            _print_allergens(out, found, forbidden_codes)
        else:
            _print_search(out, found)
        found_ids = {i.get("product_id") for i in found.get("items") or []}

        product_ids = list(spec["lead"])
        missing = [p for p in product_ids if p not in found_ids]
        if missing:
            out(f"  note: {', '.join(missing)} not in the search page; requesting anyway")
        if spec["fill_from_search"]:
            for pid in SHORTLIST_PREFERENCE:
                if len(product_ids) >= MAX_PRODUCT_IDS:
                    break
                if pid in found_ids and pid not in product_ids:
                    product_ids.append(pid)

        line = {
            "product_ids": product_ids,
            "quantity": {"value": 1000, "unit": "L"},
            "requirements": {"required_claims": list(MILK_CLAIMS)},
            "fulfilment": "allow_split",
            "substitution": "allowed",
        }
        call(f"request_offer(delivery_date={delivery!r}, lines=[product_ids={product_ids}, "
             f"quantity=1000 L, required_claims={MILK_CLAIMS}, allow_split, substitution "
             f"allowed])")
        try:
            data = client.request_offer(delivery, [line])
        except SignatureVerificationError as e:
            raise ScriptStop("signature", f"Offer signature verification FAILED: {e}. The offer "
                             "is not used; stopping before dry-run.") from None
        except HandshakeAPIError as e:
            if expect_refusal and e.status == 422 and e.code == "no_candidates":
                return _refusal(out, summary, e)
            raise
        if expect_refusal:
            # An offer under a milk-allergy mandate is the failure this scenario guards against.
            _print_offer(out, data)
            out("FAIL: the gateway issued an offer for lactose-free milk under this mandate; "
                "expected a refusal (422 no_candidates). Stopping: no dry-run, no order.")
            summary["offer_id"] = ((data.get("options") or [{}])[0].get("offer") or {}).get(
                "offer_id")
            summary["error"] = {"type": "unexpected_offer",
                                "message": "an offer was issued; expected 422 no_candidates"}
            return summary
        option = _print_offer(out, data)
        offer = option["offer"]
        offer_id = offer["offer_id"]
        summary["offer_id"] = offer_id
        summary["lines"] = [
            {"product_id": ln.get("product_id"), "role": ln.get("role"),
             "quantity": _num((ln.get("quantity") or {}).get("value")),
             "sales_unit": ln.get("sales_unit"),
             "assurance": (ln.get("availability") or {}).get("assurance")}
            for ln in offer.get("lines") or []
        ]

        call(f"dry_run_order(offer_id={offer_id!r})")
        dry = client.dry_run_order(offer_id)
        summary["dry_run_outcome"] = dry.get("outcome")
        approver = (dry.get("approver") or {}).get("name")
        out(f"  -> outcome {dry.get('outcome')}"
            + (f" (approver {approver})" if dry.get("outcome") == "needs_approval" else ""))
        for r in _reasons(dry.get("reasons")):
            out(f"     reason {r}")

        reference = f"PO-{delivery}-{spec['reference_suffix']}"
        call(f"place_order(offer_id={offer_id!r}, reference={reference!r})")
        order = client.place_order(offer_id, reference=reference)
        summary["order_id"] = order.get("order_id")
        summary["outcome"] = order.get("outcome")
        _print_order(out, order)

        status = order.get("status")
        if status == "pending_approval":
            appr = order.get("approval") or {}
            who = (appr.get("approver") or {}).get("name") or approver or "the approver"
            out(f"  Needs approval from {who}; the request expires "
                f"{_local(appr.get('expires_at'))}. Waiting for a human decision "
                f"(approval link is sent out of band; this agent cannot approve).")
            call(f"get_order(order_id={order['order_id']!r}) every {_num(poll_interval)} s, "
                 f"up to {_num(poll_timeout)} s")
            deadline = time.monotonic() + poll_timeout
            while status == "pending_approval" and time.monotonic() < deadline:
                time.sleep(max(0.0, min(poll_interval, deadline - time.monotonic())))
                order = client.get_order(order["order_id"])
                status = order.get("status")
                _dot(out)
            out("")
            _print_order(out, order)
            appr = order.get("approval") or {}
            if appr.get("status") and appr.get("status") != "pending":
                out(f"     approval {appr['status']} by {(appr.get('approver') or {}).get('name')}"
                    f" at {_local(appr.get('decided_at'))}")
        summary["final_status"] = status
        summary["outcome"] = order.get("outcome")

        if status not in FINAL_STATUSES:
            out(f"Still {status} after {_num(poll_timeout)} s. Stopping; the order stays open "
                f"until the approver decides or the approval expires.")
            return summary

        call(f"get_receipt(order_id={order['order_id']!r})")
        try:
            receipt = client.get_receipt(order["order_id"])
        except HandshakeAPIError as e:
            if e.status != 404:
                raise
            out("  -> Receipt not available yet")
        else:
            n = len(receipt.get("events") or receipt.get("timeline") or [])
            out(f"  -> receipt for order {order['order_id']}" + (f": {n} events" if n else ""))
            url = (receipt.get("links") or {}).get("html") or receipt.get("url")
            if url:
                out(f"     {url}")

        appr = order.get("approval") or {}
        codes = ", ".join(r.get("code", "") for r in order.get("reasons") or [])
        if appr.get("status") in ("approved", "declined", "expired"):
            detail = f" (approval {appr['status']}; reason {codes})"
        else:
            detail = f" ({codes})" if codes else ""
        out(f"Result: order {order['order_id']} {status}{detail}")
        return summary

    except ScriptStop as e:
        out(f"STOP: {e.message}")
        summary["error"] = {"type": e.kind, "message": e.message, **e.details}
        return summary
    except HandshakeAPIError as e:
        p = e.problem
        out(f"ERROR: gateway answered {e.status} {e.code or ''}: "
            f"{p.get('detail') or p.get('title') or ''}".rstrip())
        if p.get("next"):
            out(f"  next: {p['next']}")
        summary["error"] = {"type": "api", "status": e.status, "code": e.code,
                            "detail": p.get("detail"), "next": p.get("next")}
        return summary
    except SignatureVerificationError as e:
        out(f"STOP: signature verification failed: {e}")
        summary["error"] = {"type": "signature", "message": str(e)}
        return summary
    except httpx.HTTPError as e:
        out(f"ERROR: cannot reach the gateway: {type(e).__name__}: {e}")
        summary["error"] = {"type": "network", "message": str(e)}
        return summary


# ---------------------------------------------------------------- CLI


def resolve_key(key: str | None, agent: str | None) -> str:
    if key:
        return key
    if not agent and os.environ.get("HANDSHAKE_AGENT_KEY"):
        return os.environ["HANDSHAKE_AGENT_KEY"]
    name = agent or "nordlys"
    try:
        stored = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    entry = stored.get(name) or {}
    if not entry.get("key"):
        raise SystemExit(f"No key for agent {name!r}: pass --key, set HANDSHAKE_AGENT_KEY, or run "
                         f"scripts/seed_demo.py to write {KEYS_FILE}.")
    return entry["key"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    ap.add_argument("--base-url", default=os.environ.get("HANDSHAKE_BASE_URL", DEFAULT_BASE_URL))
    ap.add_argument("--key", help="agent bearer key")
    ap.add_argument("--agent", help="demo agent name in keys/demo_agents.json (default nordlys; "
                                    "koivula for allergy_guard)")
    ap.add_argument("--delivery-date", type=date.fromisoformat,
                    help="YYYY-MM-DD (default: next Monday, Europe/Helsinki)")
    ap.add_argument("--poll-timeout", type=float, default=120.0,
                    help="seconds to wait for an approval decision (default 120)")
    ap.add_argument("--poll-interval", type=float, default=MIN_POLL_INTERVAL,
                    help="seconds between get_order calls; at least 5")
    ap.add_argument("--json", action="store_true", help="print a final JSON summary line")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    agent = args.agent
    if not args.key and not agent:  # allergy_guard runs as Koivula unless told otherwise
        agent = SCENARIOS[args.scenario].get("default_agent")
    key = resolve_key(args.key, agent)
    delivery = args.delivery_date or next_monday()
    interval = max(MIN_POLL_INTERVAL, args.poll_interval)
    with HandshakeClient(args.base_url, key) as client:
        try:
            summary = run(args.scenario, client, delivery, args.poll_timeout, interval)
        except Exception as e:  # noqa: BLE001 - network errors etc.: one readable line
            print(f"ERROR: {type(e).__name__}: {e}")
            summary = {"scenario": args.scenario, "error": {"type": "exception",
                                                            "message": str(e)}}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    if summary.get("error"):
        return 1
    if args.scenario == "allergy_guard":  # only the expected refusal is a success
        return 0 if summary.get("final_status") == REFUSED else 1
    return 0 if summary.get("final_status") in FINAL_STATUSES else 3


if __name__ == "__main__":
    raise SystemExit(main())
