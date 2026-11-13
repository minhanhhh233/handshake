"""Policy engine (document 05, section 3; task T-201). A pure function: no I/O, no clock.

INTERFACE (shared contract):

    evaluate(offer, mandate, agent_status, facts, week_total, now, requirements) -> Evaluation

- offer: the signed Offer (lines, totals, delivery_date).
- mandate: the mandate current at evaluation time.
- agent_status: "active" or "suspended".
- facts: product_id -> ProductFacts for every product in the offer (server-side data).
- week_total: confirmed + pending order totals this ISO week (R7; R7 is cut, pass Decimal(0)).
- now: evaluation time (aware datetime), for R1's validity window.
- requirements: request_line -> Requirements from the agent's offer request (R5).

Rules built: R1, R2, R3, R4, R5, R6, R8, R9. R7 and R10 are cut (document 08, section 0):
their mandate fields (`limits.max_weekly_total_excl_tax`, `stock.require_confirmed`) are
accepted but ignored.
Outcome: rejected if any reason has effect rejected, else needs_approval if any, else accepted.
Reasons are ordered by rule number, then line_no (offer-level reasons first).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .models import (
    Evaluation,
    Mandate,
    Offer,
    OfferLine,
    ProductFacts,
    Reason,
    Requirements,
    money,
)
from .timeutil import HELSINKI

#: GS1 allergen names for messages (document 06, section 3.3; inferred, A-06).
ALLERGEN_NAMES = {
    "AM": "milk", "ML": "lactose", "AW": "cereals containing gluten", "UW": "wheat",
    "AE": "eggs", "AN": "tree nuts", "AP": "peanuts", "AY": "soybeans", "AF": "fish",
    "AC": "crustaceans", "UM": "molluscs", "AS": "sesame", "BM": "mustard", "BC": "celery",
    "AU": "sulphites", "NL": "lupin",
}

WEEKDAY_NAMES = {
    1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday", 5: "Friday", 6: "Saturday",
    7: "Sunday",
}

OFFICIAL_SUBSTITUTION_SOURCE = "valio_aimo_substitution_rule"


def allergen_label(code: str) -> str:
    """'milk (AM)' for a known GS1 code, else the code itself."""
    name = ALLERGEN_NAMES.get(code)
    return f"{name} ({code})" if name else code


# ================================================================ R2, R3, R4 (one product)


def mandate_product_reasons(facts: ProductFacts, mandate: Mandate) -> list[Reason]:
    """Mandate rules that look at one product only: R2, R3, R4 (document 05, section 3).

    Pure, no I/O. Returns every firing reason (R4 may have effect `needs_approval`);
    `line_no` is left unset for the caller to fill in. Also used by the offer builder.
    """
    reasons: list[Reason] = []
    pid = facts.product_id
    allowed = mandate.allowed_categories
    if allowed is not None and facts.category_code not in allowed:
        reasons.append(Reason(
            rule="R2", code="category_not_allowed", effect="rejected",
            message=(f"Product {pid} is in category {facts.category_code}, which this mandate "
                     f"does not allow."),
            details={"product_id": pid, "category": facts.category_code,
                     "allowed_categories": list(allowed)},
        ))
    fa = mandate.forbidden_allergens
    statements = facts.allergens if facts.has_allergen_data else {}
    for code in fa.codes:
        state = statements.get(code)
        if state is not None and state in fa.states:
            verb = "contains" if state == "CONTAINS" else "may contain"
            msg = f"Product {pid} {verb} {allergen_label(code)}, which this mandate forbids."
            if code == "AM" and "FREE_FROM_LACTOSE" in facts.claims:
                msg += " Lactose-free is not milk-free."
            reasons.append(Reason(
                rule="R3", code="forbidden_allergen", effect="rejected", message=msg,
                details={"product_id": pid, "allergen": code, "state": state},
            ))
        elif state is None and fa.unknown != "allow":
            effect = "rejected" if fa.unknown == "reject" else "needs_approval"
            consequence = ("rejects" if effect == "rejected"
                           else "needs approval for")
            reasons.append(Reason(
                rule="R4", code="allergen_data_missing", effect=effect,
                message=(f"Product {pid} has no allergen statement for {allergen_label(code)}; "
                         f"this mandate {consequence} products whose status for a forbidden "
                         f"allergen is unknown."),
                details={"product_id": pid, "allergen": code, "state": None},
            ))
    return reasons


# ================================================================ single rules


def _r1_mandate_inactive(mandate: Mandate, agent_status: str, now: datetime) -> list[Reason]:
    if now.tzinfo is None:
        now = now.replace(tzinfo=HELSINKI)
    if agent_status != "active":
        why = f"the agent is {agent_status}"
    elif now < mandate.valid_from:
        why = f"the mandate is not valid until {mandate.valid_from.isoformat()}"
    elif mandate.valid_to is not None and now >= mandate.valid_to:
        why = f"the mandate expired at {mandate.valid_to.isoformat()}"
    else:
        return []
    return [Reason(
        rule="R1", code="mandate_inactive", effect="rejected",
        message=f"The mandate is not in force: {why}.",
        details={
            "agent_status": agent_status,
            "valid_from": mandate.valid_from.isoformat(),
            "valid_to": mandate.valid_to.isoformat() if mandate.valid_to else None,
            "evaluated_at": now.isoformat(),
        },
    )]


def _r5_requirement_not_met(line: OfferLine, facts: ProductFacts,
                            req: Requirements | None) -> list[Reason]:
    if req is None:
        return []
    pid = facts.product_id
    missing = [c for c in req.required_claims if c not in facts.claims]
    statements = facts.allergens if facts.has_allergen_data else {}
    not_free = [a for a in req.forbidden_allergens if statements.get(a) != "FREE_FROM"]
    if not missing and not not_free:
        return []
    parts = []
    if missing:
        parts.append("lacks the required claim(s) " + ", ".join(missing))
    if not_free:
        parts.append("is not declared free from " + ", ".join(
            allergen_label(a) for a in not_free))
    return [Reason(
        rule="R5", code="requirement_not_met", effect="rejected", line_no=line.line_no,
        message=(f"Product {pid} " + " and ".join(parts)
                 + f", which request line {line.request_line} requires."),
        details={"product_id": pid, "request_line": line.request_line,
                 "missing_claims": missing, "not_free_from": not_free},
    )]


def _r6_spend_cap(offer: Offer, mandate: Mandate) -> list[Reason]:
    cap = mandate.limits.max_order_total_excl_tax
    if cap is None:
        return []
    total = Decimal(offer.totals.excl_tax)
    if total <= Decimal(cap):
        return []
    cur = mandate.limits.currency
    return [Reason(
        rule="R6", code="spend_cap_exceeded", effect="needs_approval",
        message=(f"Order total {money(total)} {cur} excl. tax exceeds the mandate cap of "
                 f"{money(cap)} {cur}."),
        details={"total_excl_tax": money(total), "cap": money(cap), "currency": cur},
    )]


def _r8_substitution(line: OfferLine, mandate: Mandate) -> list[Reason]:
    if line.role != "substitute":
        return []
    mode = mandate.substitution.auto_approve
    source = line.differences.source if line.differences is not None else None
    if mode == "any_meeting_requirements":
        return []
    if mode == "official_rules_only" and source == OFFICIAL_SUBSTITUTION_SOURCE:
        return []
    why = ("this mandate approves no substitutes automatically" if mode == "none"
           else "this mandate approves only substitutes from Valio Aimo's official "
                "substitution rules automatically")
    for_ = f" for {line.substitute_for}" if line.substitute_for else ""
    return [Reason(
        rule="R8", code="substitution_needs_approval", effect="needs_approval",
        line_no=line.line_no,
        message=f"Line {line.line_no} substitutes product {line.product_id}{for_}; {why}.",
        details={"product_id": line.product_id, "substitute_for": line.substitute_for,
                 "source": source, "auto_approve": mode},
    )]


def _r9_delivery_day(offer: Offer, mandate: Mandate) -> list[Reason]:
    allowed = sorted(set(mandate.delivery.allowed_weekdays))
    wd = offer.delivery_date.isoweekday()
    if wd in allowed:
        return []
    names = ", ".join(WEEKDAY_NAMES[d] for d in allowed) or "none"
    return [Reason(
        rule="R9", code="delivery_day_not_allowed", effect="needs_approval",
        message=(f"Delivery on {WEEKDAY_NAMES[wd]} {offer.delivery_date.isoformat()} is not "
                 f"on an allowed delivery day for this mandate (allowed: {names})."),
        details={"delivery_date": offer.delivery_date.isoformat(), "weekday": wd,
                 "allowed_weekdays": allowed},
    )]


# ================================================================ evaluate


def _sort_key(r: Reason) -> tuple[int, int]:
    return int(r.rule[1:]), r.line_no if r.line_no is not None else 0


def outcome_of(reasons: list[Reason]) -> str:
    """rejected > needs_approval > accepted (document 05, section 3)."""
    if any(r.effect == "rejected" for r in reasons):
        return "rejected"
    if any(r.effect == "needs_approval" for r in reasons):
        return "needs_approval"
    return "accepted"


def evaluate(
    offer: Offer,
    mandate: Mandate,
    agent_status: str,
    facts: dict[str, ProductFacts],
    week_total: Decimal,
    now: datetime,
    requirements: dict[int, Requirements] | None = None,
) -> Evaluation:
    """Evaluate every built rule and report every firing one. Pure and deterministic.

    `week_total` is accepted for the interface but unused (R7 is cut). A line whose product
    is missing from `facts` is evaluated fail-closed as an unknown product: unknown category,
    no allergen data, no claims.
    """
    reasons: list[Reason] = []
    reasons += _r1_mandate_inactive(mandate, agent_status, now)
    reqs = requirements or {}
    for line in offer.lines:
        pf = facts.get(line.product_id) or ProductFacts(
            product_id=line.product_id, category_code="unknown")
        for r in mandate_product_reasons(pf, mandate):
            reasons.append(r.model_copy(update={"line_no": line.line_no}))
        reasons += _r5_requirement_not_met(line, pf, reqs.get(line.request_line))
        reasons += _r8_substitution(line, mandate)
    reasons += _r6_spend_cap(offer, mandate)
    reasons += _r9_delivery_day(offer, mandate)
    reasons.sort(key=_sort_key)  # stable: same-rule, same-line reasons keep code order
    return Evaluation(outcome=outcome_of(reasons), reasons=reasons)  # type: ignore[arg-type]


__all__ = [
    "ALLERGEN_NAMES", "WEEKDAY_NAMES", "allergen_label", "evaluate", "mandate_product_reasons",
    "outcome_of",
]
