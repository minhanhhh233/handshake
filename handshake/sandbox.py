"""Public demo personas and per-visitor sandboxes (document 09, section 6c).

No login. A visitor's browser holds a signed cookie naming its sandbox. On first use of a
persona the gateway creates that sandbox's own copy of the persona's customer, agent and
mandate; everything they do (holds, orders, weekly totals, events) is scoped to those parties.
Holds are additionally counted per sandbox (offers.held_quantities). A sandbox expires 2 hours
after it was created: its agents' keys stop working (deps.lookup_agent) and the page starts a
new sandbox.

Agent keys are derived from ADMIN_KEY and the agent ID; only their SHA-256 is stored, and they
are never sent to the browser or to the model.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from . import db, mandates, public_demo, timeutil
from .api.deps import key_hash
from .config import get_settings
from .ids import is_id, new_id
from .models import MandateBody

TTL = timedelta(hours=2)
COOKIE = "hs_sbx"
#: New sandboxes per visitor IP per hour (bounds rows created by anonymous visitors).
MAX_NEW_PER_HOUR = 10
MAX_CAP = Decimal("100000")
DEMO_APPROVER = [{"name": "You (demo approver)", "contact": None}]
#: The school mandate's disallowed category (stage 1): energy and sports drinks, 78 products
#: in the challenge data (Red Bull, Monster, protein milkshakes).
SCHOOL_FORBIDDEN_CATEGORY = "22701"
CATEGORY_NAMES = {"22701": "energy and sports drinks"}

#: Buyer personas, in bar order. `mandate` is the document 05 mandate body (approver: you).
PERSONAS: dict[str, dict[str, Any]] = {
    "visitor": {
        "label": "Visitor", "customer": None, "agent": None,
        "blurb": "An agent with no key. It can search the catalogue; prices, availability "
                 "and offers are refused.",
    },
    "hotel": {
        "label": "Hotel chain", "customer": "Revontuli Hotels",
        "agent": "Revontuli purchasing agent",
        "blurb": "Large orders, every category, any delivery day.",
        "mandate": {"limits": {"max_order_total_excl_tax": "1000.00"},
                    "delivery": {"allowed_weekdays": [1, 2, 3, 4, 5, 6, 7]},
                    "preferences": {"preferred_labels": ["GOODS_FROM_FINLAND_BLUE_SWAN"]}},
    },
    "restaurant": {
        "label": "Restaurant", "customer": "Bistro Kallio", "agent": "Bistro Kallio agent",
        "blurb": "Small orders under a per-order and a weekly cap, weekday deliveries.",
        "mandate": {"limits": {"max_order_total_excl_tax": "300.00",
                               "max_weekly_total_excl_tax": "800.00"},
                    "delivery": {"allowed_weekdays": [1, 2, 3, 4, 5]}},
    },
    "school": {
        "label": "School kitchen", "customer": "Metsälä School Kitchen",
        "agent": "Metsälä school agent",
        "blurb": "No milk, no tree nuts, nothing with unknown allergen data, no energy drinks.",
        "mandate": {"limits": {"max_order_total_excl_tax": "600.00"},
                    "forbidden_categories": [SCHOOL_FORBIDDEN_CATEGORY],
                    "forbidden_allergens": {"codes": ["AM", "AN"],
                                            "states": ["CONTAINS", "MAY_CONTAIN"],
                                            "unknown": "reject"},
                    "delivery": {"allowed_weekdays": [1, 2, 3, 4, 5]}},
    },
    "hospital": {
        "label": "Hospital kitchen", "customer": "Saari Hospital Kitchen",
        "agent": "Saari hospital agent",
        "blurb": "Strict allergens (peanuts, tree nuts, sesame, crustaceans, molluscs; unknown "
                 "rejected) and no substitute without approval.",
        "mandate": {"limits": {"max_order_total_excl_tax": "1500.00"},
                    "forbidden_allergens": {"codes": ["AP", "AN", "AS", "AC", "UM"],
                                            "states": ["CONTAINS", "MAY_CONTAIN"],
                                            "unknown": "reject"},
                    "substitution": {"auto_approve": "none"},
                    "delivery": {"allowed_weekdays": [1, 2, 3, 4, 5, 6, 7]}},
    },
    "probation": {
        "label": "New agent on probation", "customer": "Pilot Café Oy",
        "agent": "Pilot Café agent (probation)",
        "blurb": "A newly registered agent: anything over 50 EUR needs a person's approval.",
        "mandate": {"limits": {"max_order_total_excl_tax": "50.00"},
                    "delivery": {"allowed_weekdays": [1, 2, 3, 4, 5]}},
    },
}
#: Non-buyer roles shown in the bar; their views arrive in later stages.
ROLES = {"approver": "Approver", "operations": "Valio Aimo operations"}
BUYERS = [k for k in PERSONAS if k != "visitor"]


class SandboxError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ---------------------------------------------------------------- cookie and sandbox


def _mac(text: str) -> str:
    d = hmac.new(get_settings().admin_key.encode(), text.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(d).decode().rstrip("=")[:32]


def cookie_value(sandbox_id: str) -> str:
    return f"{sandbox_id}.{_mac('sandbox:' + sandbox_id)}"


def parse_cookie(value: str | None) -> str | None:
    """The sandbox ID from a cookie value if its MAC is right, else None."""
    if not value or "." not in value or not get_settings().admin_key:
        return None
    sid, mac = value.split(".", 1)
    if not is_id(sid, "sbx") or not hmac.compare_digest(mac, _mac("sandbox:" + sid)):
        return None
    return sid


def active(sandbox_id: str | None) -> dict[str, Any] | None:
    """The sandbox row if it exists and has not expired."""
    if not sandbox_id:
        return None
    with db.connection() as conn:
        row = db.q(conn, "SELECT sandbox_id, created_at, expires_at FROM sandboxes "
                         "WHERE sandbox_id = :s", s=sandbox_id).mappings().first()
    if row is None:
        return None
    exp = timeutil.from_db(row["expires_at"])
    if exp <= timeutil.now():
        return None
    return {"sandbox_id": row["sandbox_id"], "expires_at": exp}


def create(ip: str) -> dict[str, Any]:
    if not get_settings().admin_key:
        raise public_demo.DemoUnavailable()
    h = public_demo.ip_hash(ip)
    now = timeutil.now()
    with db.transaction() as conn:
        n = db.q(conn, "SELECT COUNT(*) FROM sandboxes WHERE ip_hash = :h AND created_at > :t",
                 h=h, t=timeutil.to_db(now - timedelta(hours=1))).scalar_one()
        if n >= MAX_NEW_PER_HOUR:
            raise SandboxError(429, "too_many_sandboxes",
                               "Too many new demo sandboxes from this connection. Try again "
                               "later.")
        sid = new_id("sbx")
        db.q(conn, "INSERT INTO sandboxes (sandbox_id, ip_hash, created_at, expires_at) "
                   "VALUES (:s, :h, :c, :e)", s=sid, h=h, c=timeutil.to_db(now),
             e=timeutil.to_db(now + TTL))
    return {"sandbox_id": sid, "expires_at": now + TTL}


def ensure(cookie: str | None, ip: str) -> tuple[dict[str, Any], bool]:
    """(sandbox, is_new). Reuses the cookie's sandbox while it is valid."""
    sb = active(parse_cookie(cookie))
    if sb is not None:
        return sb, False
    return create(ip), True


# ---------------------------------------------------------------- persona parties


def agent_key(agent_id: str) -> str:
    """A sandbox agent's key: derived, never stored in clear, never sent anywhere."""
    return "hsk_" + _mac("sandbox-agent-key:v1:" + agent_id)


def _mandate_body(slug: str, cap: str | None = None, base: dict[str, Any] | None = None
                  ) -> MandateBody:
    doc: dict[str, Any] = {
        "valid_from": "2026-11-01T00:00:00+02:00", "approvers": DEMO_APPROVER,
        **(base if base is not None else PERSONAS[slug]["mandate"]),
    }
    if cap is not None:
        doc["limits"] = {**doc.get("limits", {}), "max_order_total_excl_tax": cap}
    return MandateBody.model_validate(doc)


def party(sandbox_id: str, slug: str) -> dict[str, Any]:
    """The sandbox's customer and agent for a buyer persona, created on first use."""
    if slug not in BUYERS:
        raise SandboxError(400, "unknown_persona", "Unknown persona.")
    p = PERSONAS[slug]
    with db.transaction() as conn:
        row = db.q(conn, "SELECT a.agent_id, a.customer_id FROM customers c JOIN agents a "
                         "ON a.customer_id = c.customer_id WHERE c.sandbox_id = :s AND "
                         "c.sandbox_persona = :p ORDER BY a.created_at LIMIT 1",
                   s=sandbox_id, p=slug).first()
        if row is not None:
            return {"agent_id": row[0], "customer_id": row[1], "customer": p["customer"],
                    "persona": slug}
        now = timeutil.to_db(timeutil.now())
        cid, aid = new_id("cus"), new_id("agt")
        db.q(conn, "INSERT INTO customers (customer_id, name, created_at, sandbox_id, "
                   "sandbox_persona) VALUES (:c, :n, :t, :s, :p)",
             c=cid, n=p["customer"], t=now, s=sandbox_id, p=slug)
        db.q(conn, "INSERT INTO agents (agent_id, customer_id, name, key_sha256, status, "
                   "created_at) VALUES (:a, :c, :n, :h, 'active', :t)",
             a=aid, c=cid, n=p["agent"], h=key_hash(agent_key(aid)), t=now)
        mandates.replace(conn, aid, cid, _mandate_body(slug))
    return {"agent_id": aid, "customer_id": cid, "customer": p["customer"], "persona": slug}


def card(sandbox_id: str, slug: str) -> dict[str, Any]:
    """What the mandate card shows for a persona (creates the party on first use)."""
    if slug == "visitor":
        return {"persona": "visitor", "label": "Visitor", "blurb": PERSONAS["visitor"]["blurb"],
                "anonymous": True}
    pt = party(sandbox_id, slug)
    with db.connection() as conn:
        m = mandates.get_current(conn, pt["agent_id"])
    assert m is not None
    fa = m.forbidden_allergens
    from .policy import allergen_label

    rules = []
    lim = m.limits
    rules.append(("Per order", f"{lim.max_order_total_excl_tax} EUR excl. tax, then a person "
                               f"approves" if lim.max_order_total_excl_tax else "no cap"))
    if lim.max_weekly_total_excl_tax:
        rules.append(("Per week", f"{lim.max_weekly_total_excl_tax} EUR excl. tax, then a "
                                  f"person approves"))
    if fa.codes:
        rules.append(("Allergens", "never " + ", ".join(allergen_label(c) for c in fa.codes)
                      + {"reject": "; unknown data is refused",
                         "needs_approval": "; unknown data needs approval",
                         "allow": "; unknown data is allowed"}[fa.unknown]))
    if m.forbidden_categories:
        rules.append(("Categories", "never " + ", ".join(
            f"{CATEGORY_NAMES.get(c, 'category')} ({c})" for c in m.forbidden_categories)))
    elif m.allowed_categories is None:
        rules.append(("Categories", "all"))
    days = m.delivery.allowed_weekdays
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    rules.append(("Delivery", "any day" if len(days) == 7 else
                  ", ".join(names[d - 1] for d in sorted(days))))
    rules.append(("Substitutes", {
        "none": "every substitute needs approval",
        "official_rules_only": "Valio Aimo's official substitutes pass; others need approval",
        "any_meeting_requirements": "any substitute meeting the requirements passes",
    }[m.substitution.auto_approve]))
    return {"persona": slug, "label": PERSONAS[slug]["label"], "customer": pt["customer"],
            "agent": PERSONAS[slug]["agent"], "blurb": PERSONAS[slug]["blurb"],
            "mandate_version": m.version,
            "cap": str(lim.max_order_total_excl_tax) if lim.max_order_total_excl_tax else None,
            "rules": [{"name": n, "value": v} for n, v in rules]}


def set_cap(sandbox_id: str, slug: str, cap: Any) -> dict[str, Any]:
    """New mandate version with another per-order cap (the visitor edits it on the card)."""
    try:
        value = Decimal(str(cap)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise SandboxError(400, "invalid_cap", "The cap must be a number of euros.") from None
    if not value.is_finite() or value <= 0 or value > MAX_CAP:
        raise SandboxError(400, "invalid_cap", f"The cap must be between 0.01 and {MAX_CAP} EUR.")
    pt = party(sandbox_id, slug)
    with db.transaction() as conn:
        m = mandates.get_current(conn, pt["agent_id"])
        assert m is not None
        base = m.dump()
        for k in ("mandate_id", "version", "agent_id", "customer_id"):
            base.pop(k, None)
        mandates.replace(conn, pt["agent_id"], pt["customer_id"],
                         _mandate_body(slug, f"{value}", base))
    return card(sandbox_id, slug)


def agent_sandbox_id(agent_id: str) -> str | None:
    with db.connection() as conn:
        return db.q(conn, "SELECT c.sandbox_id FROM agents a JOIN customers c ON "
                          "c.customer_id = a.customer_id WHERE a.agent_id = :a",
                    a=agent_id).scalar()
