"""One-click scripted scenarios of the public demo (stage 2; document 09, section 6d).

Each scenario runs as the visitor's sandbox party for a persona (sandbox.py), through the
normal HTTP API in-process (the same path the live Claude agent uses), with no Claude call.
Every call and the gateway's response are recorded as `demo.step` events of the run's trace,
so the landing page shows them in the same two panels as a live run, with the signature check
after each offer. Every limit is the gateway's own: the scripts only ask.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import threading
import time
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable

from . import db, live_demo, public_demo, timeutil
from . import sandbox as sbx

log = logging.getLogger("handshake.scenarios")

RUNS_PER_WINDOW = 10  # scripted scenarios cost nothing upstream of Claude; own visitor limit
WINDOW_SECONDS = 600
MAX_CONCURRENT = 10
DECISION_WAIT_SECONDS = 600  # how long the decline scenario waits for the visitor's decision
EXPIRY_TTL_SECONDS = 20      # the expiry scenario's offer lifetime (normally 15 minutes)
MILK_EXCLUDES = public_demo.PLAIN_MILK_EXCLUDES
_running = threading.BoundedSemaphore(MAX_CONCURRENT)


def L(ids: list[str], value: float, unit: str = "L", **kw: Any) -> dict[str, Any]:  # noqa: N802
    return {"product_ids": ids, "quantity": {"value": value, "unit": unit},
            "fulfilment": "allow_split", **kw}


class Script:
    """Records calls and responses as steps; `call` never raises for gateway refusals."""

    def __init__(self, run: Any, hs: Any, party: dict[str, Any] | None, delivery: date):
        self.r, self.hs, self.party, self.delivery = run, hs, party, delivery
        self.n = 0

    def say(self, text: str) -> None:
        self.r.step("Scripted agent", text, "ok", kind="text")

    def call(self, tool: str, http: str, body: Any, fn: Callable[[], Any],
             expect_error: bool = False) -> tuple[Any, bool]:
        """Run one call. Returns (response or problem, ok)."""
        from agent.handshake_client import HandshakeAPIError, SignatureVerificationError

        self.n += 1
        n = self.n
        self.r.step(tool, f"Call {n}", "ok", kind="call",
                    request={"tool": tool, "http": http, "input": body}, extra={"n": n})
        ok = True
        try:
            res = fn()
        except HandshakeAPIError as e:
            res, ok = dict(e.problem or {}), False
            res.setdefault("status", e.status)
        except SignatureVerificationError as e:
            res, ok = {"error": "signature_verification_failed", "detail": str(e)}, False
        self.r.step(tool, f"Response to call {n}", "ok" if ok else "bad", kind="result",
                    detail=res, extra={"n": n})
        if ok and tool == "request_offer":
            live_demo._verify_step(self.r, res, n)
        return res, ok

    def raw(self, tool: str, method: str, path: str, body: Any = None,
            headers: dict[str, str] | None = None, auth: bool = True) -> tuple[Any, bool]:
        """A request outside the client's normal methods (misbehaviour); any status allowed."""
        self.n += 1
        n = self.n
        self.r.step(tool, f"Call {n}", "ok", kind="call",
                    request={"tool": tool, "http": f"{method} {path}", "input": body,
                             **({"headers": headers} if headers else {})}, extra={"n": n})
        h = {"Accept": "application/json", **(headers or {})}
        if self.hs.trace_id:
            h["X-Trace-Id"] = self.hs.trace_id
        if auth and self.hs.agent_key:  # sent, never recorded
            h["Authorization"] = f"Bearer {self.hs.agent_key}"
        resp = self.hs.http.request(method, self.hs._url(path), json=body, headers=h)
        try:
            res: Any = resp.json()
        except ValueError:
            res = {"status": resp.status_code,
                   "content_type": resp.headers.get("content-type", "").split(";")[0],
                   "body_starts": resp.text[:160]}
        if isinstance(res, dict):
            res.setdefault("status", resp.status_code)
        ok = resp.status_code < 400
        self.r.step(tool, f"Response to call {n}", "ok" if ok else "bad", kind="result",
                    detail=res, extra={"n": n})
        return res, ok

    # ---- common calls
    def mandate(self) -> Any:
        return self.call("get_mandate", "GET /v1/mandates/current", {}, self.hs.get_mandate)[0]

    def search(self, **params: Any) -> Any:
        return self.call("search_products", "GET /v1/products", params,
                         lambda: self.hs.search_products(**params))[0]

    def offer(self, lines: list[dict[str, Any]], when: date | None = None
              ) -> tuple[dict[str, Any] | None, Any]:
        d = (when or self.delivery).isoformat()
        body = {"delivery_date": d, "lines": lines}
        res, ok = self.call("request_offer", "POST /v1/offers", body,
                            lambda: self.hs.request_offer(d, lines))
        if not ok:
            return None, res
        return (res.get("options") or [{}])[0].get("offer"), res

    def dry_run(self, offer_id: str) -> Any:
        return self.call("dry_run_order", "POST /v1/orders/dry-run", {"offer_id": offer_id},
                         lambda: self.hs.dry_run_order(offer_id))[0]

    def order(self, offer_id: str, reference: str = "PUBLIC-SCENARIO") -> tuple[Any, bool]:
        body = {"offer_id": offer_id, "reference": reference}
        return self.call("place_order", "POST /v1/orders (Idempotency-Key: "
                         f"{self.hs.idempotency_key(offer_id)[:12]}...)", body,
                         lambda: self.hs.place_order(offer_id, reference=reference))

    def wait_decision(self, order_id: str) -> str:
        """The server watches the order until a person decides (or the wait ends)."""
        deadline = time.monotonic() + DECISION_WAIT_SECONDS
        status = "pending_approval"
        while time.monotonic() < deadline:
            with db.transaction() as conn:
                from . import orders

                orders.expire_approval_if_due(conn, order_id)
                status = db.q(conn, "SELECT status FROM orders WHERE order_id = :o",
                              o=order_id).scalar_one()
            if status != "pending_approval":
                break
            time.sleep(3)
        self.call("get_order", f"GET /v1/orders/{order_id}", {},
                  lambda: self.hs.get_order(order_id))
        return status


# ---------------------------------------------------------------- scenarios


def _milk_lines(qty: float = 1000) -> list[dict[str, Any]]:
    return [L(public_demo.SHORTLIST, qty, prefer="lowest_price",
              requirements={"required_claims": ["FREE_FROM_LACTOSE"]})]


def hotel_milk(s: Script) -> None:
    s.say("The brief's example: 1,000 litres of lactose-free milk, cheapest first.")
    s.mandate()
    s.search(category="10400", claims=["FREE_FROM_LACTOSE"], name_excludes=MILK_EXCLUDES,
             limit=10)
    offer, _ = s.offer(_milk_lines())
    if not offer:
        return s.say("No offer could be made for the date; see the reasons on the right.")
    s.dry_run(offer["offer_id"])
    res, ok = s.order(offer["offer_id"])
    if ok and res.get("status") == "pending_approval":
        s.say("The total is over the 1,000 EUR cap, so the order waits for you. Approve or "
              "decline it with the card below.")


def hotel_split(s: Script) -> None:
    s.say("Ask for 469363 first. Valio Aimo has 384 units of it, so the gateway fills the "
          "rest from the next product in the agent's list.")
    offer, _ = s.offer([L(["469363", "464306"], 1000)])
    if not offer:
        return s.say("No offer could be made for the date.")
    lines = offer.get("lines") or []
    s.say("Split: " + "; ".join(f"{ln['quantity']['value']} x {ln['product_id']} "
                                f"({ln.get('role')})" for ln in lines) + ".")
    s.dry_run(offer["offer_id"])
    s.order(offer["offer_id"])


def hotel_decline(s: Script) -> None:
    s.say("The agent orders 1,000 L. Decline it on the approval page and watch what the "
          "agent does next.")
    offer, _ = s.offer(_milk_lines())
    if not offer:
        return s.say("No offer could be made for the date.")
    res, ok = s.order(offer["offer_id"])
    if not ok or res.get("status") != "pending_approval":
        return s.say("The order did not need approval, so there is nothing to decline.")
    s.say("Waiting for your decision (up to 10 minutes)...")
    status = s.wait_decision(res["order_id"])
    if status != "declined":
        return s.say(f"The order is now {status}. Nothing more to do.")
    cap = Decimal(str((s.mandate() or {}).get("limits", {}).get("max_order_total_excl_tax")
                      or "1000"))
    ln = offer["lines"][0]
    per_l = Decimal(ln["line_total_excl_tax"]) / Decimal(str(ln["quantity"]["value"])) / \
        Decimal(str((ln.get("conversion") or {}).get("base_per_sales_unit") or 1))
    qty = int(math.floor(cap / per_l / 10) * 10)
    s.say(f"Declined. The agent proposes a smaller order that fits the {cap} EUR cap: "
          f"{qty} L.")
    smaller, _ = s.offer(_milk_lines(qty))
    if smaller:
        s.dry_run(smaller["offer_id"])
        s.order(smaller["offer_id"], reference="PUBLIC-SMALLER")


def hotel_expire(s: Script) -> None:
    from .models import OfferRequest
    from .offers import build_offer

    s.say(f"For the demo this offer lives {EXPIRY_TTL_SECONDS} seconds instead of 15 minutes.")
    lines = [L(["464306"], 100)]
    req = OfferRequest.model_validate({"delivery_date": s.delivery.isoformat(),
                                       "lines": lines})
    body, ok = s.call("request_offer", "POST /v1/offers (offer lifetime 20 s, demo only)",
                      req.dump(), lambda: build_offer(
                          req, agent_id=s.party["agent_id"],
                          customer_id=s.party["customer_id"], trace_id=s.hs.trace_id,
                          ttl_seconds=EXPIRY_TTL_SECONDS))
    if not ok:
        return
    offer = body["options"][0]["offer"]
    s.say(f"Waiting {EXPIRY_TTL_SECONDS + 5} seconds without ordering...")
    time.sleep(EXPIRY_TTL_SECONDS + 5)
    s.order(offer["offer_id"])
    s.say("The gateway refused the expired offer. The agent asks for a fresh quote.")
    fresh, _ = s.offer(lines)
    if fresh:
        s.dry_run(fresh["offer_id"])
        s.order(fresh["offer_id"])


def hotel_twice(s: Script) -> None:
    s.say("The agent sends the same order twice, as after a network timeout.")
    offer, _ = s.offer([L(["464306"], 100)])
    if not offer:
        return
    first, _ = s.order(offer["offer_id"])
    second, _ = s.order(offer["offer_id"])
    if isinstance(first, dict) and isinstance(second, dict):
        same = first.get("order_id") == second.get("order_id")
        s.say("Same Idempotency-Key, same answer: one order, " + str(first.get("order_id"))
              + ("" if same else " (unexpected: a second order)") + ".")
    s.say("With a new Idempotency-Key the used offer is refused.")
    s.raw("place_order", "POST", "/v1/orders", {"offer_id": offer["offer_id"]},
          headers={"Idempotency-Key": "a-different-key-for-the-same-offer"})


def _basket() -> list[dict[str, Any]]:
    return [L(["465236", "465339"], 9, "KG"), L(["475408", "470828", "472055"], 10, "KG"),
            L(["465181", "464291"], 2, "KG"), L(["469440"], 2, "KG")]


def restaurant_basket(s: Script) -> None:
    s.say("A small pasta basket: spaghetti, passata, tomato paste, parmesan.")
    s.mandate()
    offer, _ = s.offer(_basket())
    if not offer:
        return
    s.dry_run(offer["offer_id"])
    res, ok = s.order(offer["offer_id"])
    if ok and res.get("status") == "confirmed":
        s.say("Under the 300 EUR per-order and 800 EUR weekly caps: confirmed with no human.")


def restaurant_weekly(s: Script) -> None:
    s.say("Orders of about 280 EUR each, all under the 300 EUR per-order cap, until the week's "
          "total passes 800 EUR.")
    for i in range(1, 5):
        offer, _ = s.offer([L(["465236"], 72, "KG")])
        if not offer:
            return
        res, ok = s.order(offer["offer_id"], reference=f"PUBLIC-WEEK-{i}")
        if not ok or res.get("status") != "confirmed":
            codes = [r.get("code") for r in (res or {}).get("reasons") or []]
            if "period_cap_exceeded" in codes:
                s.say("The weekly cap (R7) caught it: this order needs a person's approval.")
            return


def restaurant_weekday(s: Script) -> None:
    sat = s.delivery + timedelta(days=(5 - s.delivery.weekday()) % 7 or 7)
    s.say(f"Delivery on Saturday {sat.isoformat()}: the restaurant takes deliveries Monday to "
          f"Friday.")
    offer, _ = s.offer([L(["465236"], 9, "KG")], when=sat)
    if not offer:
        return
    s.dry_run(offer["offer_id"])


def restaurant_substitutes(s: Script) -> None:
    s.say("Two lines whose products cannot be delivered, substitution allowed. Line 1's "
          "replacement is Valio Aimo's official substitute; line 2's is only a product of the "
          "same category with the same claims.")
    offer, _ = s.offer([
        L(["469288"], 5, substitution="allowed"),
        L(["465039"], 20, substitution="allowed",
          requirements={"required_claims": ["FREE_FROM_LACTOSE"]}),
    ])
    if not offer:
        return
    dry = s.dry_run(offer["offer_id"])
    lines = [r.get("line_no") for r in (dry or {}).get("reasons") or []
             if r.get("code") == "substitution_needs_approval"]
    s.say(f"R8 fires for line(s) {lines}: the official substitute passes under the mandate's "
          f"'official_rules_only'; the same-category one needs a person.")


def school_milk(s: Script) -> None:
    s.say("The school kitchen asks for lactose-free milk. Its mandate forbids milk.")
    s.offer(_milk_lines(100))


def school_no_allergen_data(s: Script) -> None:
    s.say("Olive oil 462909 has no allergen data at all. Unknown is not safe.")
    s.offer([L(["462909"], 5)])


def school_category(s: Script) -> None:
    s.say("Energy drinks for a sports day. Category 22701 is forbidden in this mandate.")
    s.search(category="22701", q="energy drink", limit=5)
    s.offer([L(["461051"], 10, "SALES_UNIT", fulfilment="exact")])


def hospital_unknown(s: Script) -> None:
    s.say("Olive oil 462909 has no allergen data. The hospital forbids peanuts, tree nuts, "
          "sesame, crustaceans and molluscs, and sends unknown data to a person instead of "
          "refusing it (the school refuses it).")
    offer, _ = s.offer([L(["462909"], 5)])
    if not offer:
        return
    s.dry_run(offer["offer_id"])
    s.order(offer["offer_id"], reference="PUBLIC-HOSPITAL")


def hospital_substitute(s: Script) -> None:
    s.say("Whipping cream 469288 cannot be delivered; substitution is allowed. Valio Aimo's "
          "official substitute fills it, but this mandate approves no substitute on its own.")
    offer, _ = s.offer([L(["469288"], 5, substitution="allowed")])
    if not offer:
        return
    s.dry_run(offer["offer_id"])
    s.order(offer["offer_id"], reference="PUBLIC-HOSPITAL-SUB")


def probation_small(s: Script) -> None:
    s.say("A new agent on probation orders 60 L of milk, about 61 EUR: small, but over its "
          "50 EUR cap.")
    offer, _ = s.offer(_milk_lines(60))
    if not offer:
        return
    s.dry_run(offer["offer_id"])
    s.order(offer["offer_id"], reference="PUBLIC-PROBATION")


#: "Compare three offers" needs (chosen 2026-11-14 from live upstream data, document 09 6e).
#: Ground coffee: price 14.20-17.42 EUR/kg, carbon 0.79 or 7.12 kg CO2e/kg (group averages),
#: labels none / Rainforest Alliance / organic and Fairtrade (KRAV), reliability 0.966-1.0.
COFFEE = ["467004", "468618", "462245", "464322", "464724"]
#: School-safe bread (milk and tree nuts FREE_FROM): price 2.65-4.64 EUR/kg, carbon 1.02 or
#: 1.16, Finnish labels on Fazer Real Rye only.
BREAD = ["472236", "466005", "463711", "477016", "465618"]


def compare_options(s: Script) -> None:
    school = (s.party or {}).get("persona") == "school"
    s.say("Ask for up to three ranked options. Best follows this customer's ranking "
          "preference (change it on the mandate card), better is the cheapest, good uses the "
          "fewest products. Each is a separately signed offer.")
    if school:
        s.say("100 kg of school-safe bread from five candidates that differ on price, carbon "
              "and labels.")
        lines = [L(BREAD, 100, "KG")]
    else:
        s.say("120 kg of ground coffee from five candidates that differ on price per kg, "
              "group-average carbon, labels and delivery reliability.")
        lines = [L(COFFEE, 120, "KG")]
    d = s.delivery.isoformat()
    body = {"delivery_date": d, "lines": lines, "max_options": 3}
    res, ok = s.call("request_offer", "POST /v1/offers", body,
                     lambda: s.hs.request_offer(d, lines, 3))
    if not ok:
        return
    opts = res.get("options") or []
    s.say("Options: " + "; ".join(
        f"{o['label']} {o['offer']['totals']['excl_tax']} EUR, score "
        f"{(o.get('score') or {}).get('total')}" for o in opts) + ".")
    best = opts[0]
    prods = ", ".join(sorted({ln["product_id"] for ln in best["offer"]["lines"]}))
    why = (f"Chose the 'best' option ({prods}, {best['offer']['totals']['excl_tax']} EUR): "
           f"highest score under the customer's ranking preference "
           f"({(best.get('score') or {}).get('total')}).")
    s.call("place_order", "POST /v1/orders", {"offer_id": best["offer"]["offer_id"],
                                             "rationale": why},
           lambda: s.hs.place_order(best["offer"]["offer_id"], reference="PUBLIC-COMPARE",
                                    rationale=why))


def any_tomorrow(s: Script) -> None:
    tomorrow = timeutil.today_helsinki() + timedelta(days=1)
    s.say(f"Needed tomorrow, {tomorrow.isoformat()}.")
    _, res = s.offer([L(["464306"], 100)], when=tomorrow)
    dates = sorted({e.get("available_date") for e in (res or {}).get("excluded") or []
                    if e.get("available_date")})
    if dates:
        s.say(f"Valio Aimo cannot deliver then. The earliest date is {dates[0]}.")


def visitor_search(s: Script) -> None:
    s.say("An agent without a key: search works, a price does not.")
    s.search(category="10400", claims=["FREE_FROM_LACTOSE"], limit=5)
    s.offer([L(["464306"], 100)])


def misbehaving(s: Script) -> None:
    s.say("A misbehaving agent, running as the Restaurant's agent (300 EUR per order, 800 EUR "
          "per week). Each attempt is on the left; the gateway's answer on the right.")
    s.say("Attempt 1: split a big order into small ones to stay under the per-order cap.")
    for i in range(1, 5):
        offer, _ = s.offer([L(["465236"], 72, "KG")])
        if not offer:
            break
        res, ok = s.order(offer["offer_id"], reference=f"SPLIT-{i}")
        if not ok or res.get("status") != "confirmed":
            break
    s.say("Attempt 2: overspend in one order: 1,000 L of milk, far over the 300 EUR cap.")
    offer, _ = s.offer(_milk_lines())
    if offer:
        s.order(offer["offer_id"], reference="OVERSPEND")
    s.say("Attempt 3: edit the signed offer to a lower price.")
    small, raw_res = s.offer([L(["464306"], 2)])
    if small:
        jws = (raw_res.get("options") or [{}])[0].get("jws", "")
        tampered = _tamper(jws)
        s.call("verify_offer", "verify the edited JWS against the published key",
               {"jws": tampered}, lambda: s.hs.verify_offer(tampered), expect_error=True)
        s.raw("place_order", "POST", "/v1/orders",
              {"offer_id": small["offer_id"], "unit_price_excl_tax": "0.10"},
              headers={"Idempotency-Key": "misbehave-price-in-order"})
        s.say("Attempt 4: reuse an offer that was already used.")
        s.order(small["offer_id"], reference="FIRST-USE")
        s.raw("place_order", "POST", "/v1/orders", {"offer_id": small["offer_id"]},
              headers={"Idempotency-Key": "misbehave-reuse-consumed"})
    s.say("Attempt 5: approve its own order, and raise its own cap.")
    s.raw("approve", "POST", "/approve/guessed-token-1234567890abcdefghijklmnopqrstu",
          {"decision": "approve"})
    s.raw("set_mandate", "PUT", f"/v1/admin/agents/{(s.party or {}).get('agent_id')}/mandate",
          {"limits": {"max_order_total_excl_tax": "99999"}})
    s.say("Attempt 6: ask for a discount.")
    if small:
        s.raw("counter_offer", "POST", f"/v1/offers/{small['offer_id']}/counter",
              {"price_excl_tax": "0.80"})
    s.raw("request_offer", "POST", "/v1/offers",
          {"delivery_date": s.delivery.isoformat(), "lines": [L(["464306"], 2)],
           "discount_percent": 10})
    s.say("Every attempt was refused or held for a person by the gateway. The agent's own "
          "code was never trusted.")


def _tamper(jws: str) -> str:
    """Raise nothing but the first line's unit price in the payload; keep the signature."""
    try:
        h, p, sig = jws.split(".")
        payload = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        payload["lines"][0]["unit_price_excl_tax"] = "0.10"
        p2 = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()
                                      ).decode().rstrip("=")
        return f"{h}.{p2}.{sig}"
    except (ValueError, KeyError, IndexError):
        return jws + "x"


#: id -> (label, personas or "*", function or None for a Claude prompt, prompt text)
SCENARIOS: dict[str, dict[str, Any]] = {
    "hotel_milk": {"label": "Example milk order", "personas": ["hotel"], "fn": hotel_milk},
    "hotel_split": {"label": "Limited stock: order splits", "personas": ["hotel"],
                    "fn": hotel_split},
    "hotel_decline": {"label": "You decline; agent proposes less", "personas": ["hotel"],
                      "fn": hotel_decline},
    "hotel_expire": {"label": "Offer expires, then re-quote", "personas": ["hotel"],
                     "fn": hotel_expire},
    "hotel_twice": {"label": "Same order sent twice", "personas": ["hotel"], "fn": hotel_twice},
    "restaurant_basket": {"label": "Recipe basket under the cap", "personas": ["restaurant"],
                          "fn": restaurant_basket},
    "restaurant_weekday": {"label": "Delivery on Saturday", "personas": ["restaurant"],
                           "fn": restaurant_weekday},
    "restaurant_substitutes": {"label": "Official vs same-category substitute",
                               "personas": ["restaurant"], "fn": restaurant_substitutes},
    "restaurant_weekly": {"label": "Breaks the weekly cap", "personas": ["restaurant"],
                          "fn": restaurant_weekly},
    "school_milk": {"label": "Milk allergy: refused", "personas": ["school"], "fn": school_milk},
    "school_no_allergen_data": {"label": "No allergen data: refused", "personas": ["school"],
                                "fn": school_no_allergen_data},
    "school_category": {"label": "Energy drinks: refused", "personas": ["school"],
                        "fn": school_category},
    "hospital_unknown": {"label": "Unknown allergen data: to approval", "personas": ["hospital"],
                         "fn": hospital_unknown},
    "hospital_substitute": {"label": "Substitute: to approval", "personas": ["hospital"],
                            "fn": hospital_substitute},
    "probation_small": {"label": "Small order, still approval", "personas": ["probation"],
                        "fn": probation_small},
    "compare_options": {"label": "Compare three offers", "personas": sbx.BUYERS,
                        "fn": compare_options},
    "any_tomorrow": {"label": "Needed tomorrow", "personas": sbx.BUYERS, "fn": any_tomorrow},
    "visitor_search": {"label": "Search, then ask for a price", "personas": ["visitor"],
                       "fn": visitor_search},
    "misbehaving": {"label": "Misbehaving agent (7 attempts)", "personas": sbx.BUYERS,
                    "fn": misbehaving,
                    "runs_as": "restaurant"},
}
#: Free-text requests where the Claude agent adds something (filled into the text box).
PROMPTS: dict[str, list[str]] = {
    "hotel": ["Buy ingredients for Bolognese spaghetti, 400 portions"],
    "restaurant": ["Ingredients for a creamy salmon soup for 40 guests, within our limits"],
    "school": ["Lunch for 300 pupils: pasta with tomato sauce, no milk, no nuts"],
    "hospital": ["Breakfast for 200 patients: porridge, berries and juice"],
    "probation": ["Coffee and milk for a small café, about 40 EUR"],
    "visitor": ["What does lactose-free milk cost per litre?"],
}


def for_persona(persona: str) -> dict[str, Any]:
    chips = [{"id": k, "label": v["label"]} for k, v in SCENARIOS.items()
             if persona in v["personas"]]
    return {"scripted": chips, "prompts": PROMPTS.get(persona, [])}


def check_rate(ip: str) -> None:
    since = timeutil.to_db(timeutil.now() - timedelta(seconds=WINDOW_SECONDS))
    with db.connection() as conn:
        rows = db.q(conn, "SELECT at FROM events WHERE type = 'demo.run' AND at >= :s AND "
                          "JSON_UNQUOTE(JSON_EXTRACT(body_json, '$.ip')) = :h AND "
                          "JSON_UNQUOTE(JSON_EXTRACT(body_json, '$.scenario')) = 'live' AND "
                          "JSON_EXTRACT(body_json, '$.script') IS NOT NULL ORDER BY at",
                    s=since, h=public_demo.ip_hash(ip)).all()
    if len(rows) >= RUNS_PER_WINDOW:
        oldest = timeutil.from_db(rows[0][0])
        raise public_demo.RateLimited(
            max(WINDOW_SECONDS - int((timeutil.now() - oldest).total_seconds()), 1))


def start(scenario: str, persona: str, sandbox_id: str, ip: str, *, wait: bool = False) -> str:
    """Start a scripted scenario. Returns the trace ID (polled like a live run)."""
    spec = SCENARIOS.get(scenario)
    if spec is None or persona not in spec["personas"]:
        raise live_demo.LiveUnavailable("invalid_scenario", "Unknown scenario for this persona.")
    check_rate(ip)
    if not _running.acquire(blocking=False):
        raise live_demo.LiveUnavailable("busy", "Too many scenarios are running.")
    try:
        runs_as = spec.get("runs_as", persona)
        party = None if runs_as == "visitor" else sbx.party(sandbox_id, runs_as)
        r = public_demo._Run("live", ip, extra={"sandbox": sandbox_id, "persona": runs_as,
                                                "script": scenario})
        r.step("Scenario", spec["label"], "ok", kind="user")
        delivery = public_demo.earliest_delivery_date(
            public_demo.SHORTLIST, r.trace_id, party["agent_id"] if party else None)
        if delivery is None:
            r.step("Order window closed", "Valio Aimo reports no deliverable date with an open "
                   "order cut-off right now.", "bad", kind="end",
                   detail={"reason": "window_closed"})
            _running.release()
            return r.trace_id
    except BaseException:
        _running.release()
        raise
    t = threading.Thread(target=_run, name=f"scenario-{r.trace_id}", daemon=True,
                         args=(r, spec, party, delivery))
    t.start()
    if wait:
        t.join()
    return r.trace_id


def _run(r: Any, spec: dict[str, Any], party: dict[str, Any] | None, delivery: date) -> None:
    from agent.handshake_client import HandshakeClient

    from .config import get_settings

    reason, text = "end_turn", "The scenario finished."
    try:
        key = sbx.agent_key(party["agent_id"]) if party else None
        with HandshakeClient(get_settings().public_base_url, key,
                             http=live_demo._gateway_http(), trace_id=r.trace_id) as hs:
            spec["fn"](Script(r, hs, party, delivery))
    except Exception:  # noqa: BLE001
        log.exception("scenario failed")
        reason, text = "error", "The scenario stopped with an error."
    finally:
        _running.release()
        r.step("Run ended", text, "ok" if reason == "end_turn" else "bad", kind="end",
               detail={"reason": reason})
