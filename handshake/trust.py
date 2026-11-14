"""Trust ladder (stage 5, "trusted with"; document 05, section 3; document 09, section 6h).

A mandate may carry `trust_ladder: {steps, promote_after}`. The agent's autonomous per-order
cap is then steps[level] instead of `limits.max_order_total_excl_tax`. The level is computed,
never stored, from the agent's finished orders in the order they were finished:

- a confirmed order adds one to the streak; when the streak reaches `promote_after` the level
  rises one step (if there is one) and the streak starts again;
- a rejected order (by the gateway) or a declined one (by the approver) drops the level back to
  the first step and resets the streak;
- pending and expired orders do not count.

The same history always gives the same cap, so evaluation stays deterministic (NFR-04). A change
of level is written as `trust.level_changed` in the trace of the order that caused it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from . import db, events


def state(conn: Any, agent_id: str, ladder: Any, exclude_order: str | None = None
          ) -> dict[str, Any]:
    steps = [Decimal(s) for s in ladder.steps]
    rows = db.q(conn, "SELECT order_id, status FROM orders WHERE agent_id = :a AND status IN "
                      "('confirmed', 'rejected', 'declined') ORDER BY updated_at, created_at, "
                      "order_id", a=agent_id).all()
    level = streak = 0
    for oid, status in rows:
        if oid == exclude_order:
            continue
        if status == "confirmed":
            streak += 1
            if streak >= ladder.promote_after and level < len(steps) - 1:
                level, streak = level + 1, 0
        else:
            level, streak = 0, 0
    return {"level": level, "streak": streak, "cap": steps[level], "steps": steps,
            "promote_after": ladder.promote_after, "top": level == len(steps) - 1}


def effective(conn: Any, mandate: Any) -> Any:
    """The mandate as evaluated: with a ladder, the per-order cap is the ladder's current step."""
    ladder = getattr(mandate, "trust_ladder", None)
    if ladder is None:
        return mandate
    st = state(conn, mandate.agent_id, ladder)
    limits = mandate.limits.model_copy(update={"max_order_total_excl_tax": f"{st['cap']:.2f}"})
    return mandate.model_copy(update={"limits": limits})


def note_change(conn: Any, mandate: Any, order_id: str, trace_id: str) -> None:
    """After an order finished: record a change of trust level in that order's trace."""
    ladder = getattr(mandate, "trust_ladder", None) if mandate is not None else None
    if ladder is None:
        return
    before = state(conn, mandate.agent_id, ladder, exclude_order=order_id)
    after = state(conn, mandate.agent_id, ladder)
    if before["level"] == after["level"]:
        return
    up = after["level"] > before["level"]
    why = (f"after {ladder.promote_after} confirmed orders in a row" if up
           else "after a rejected or declined order")
    events.append(
        conn, trace_id=trace_id, type="trust.level_changed", actor_type="gateway",
        audience="both",
        text=(f"Trust ladder: the agent's autonomous cap {'rose' if up else 'dropped back'} "
              f"from {before['cap']:.2f} to {after['cap']:.2f} EUR {why}."),
        body={"agent_id": mandate.agent_id, "order_id": order_id,
              "from": f"{before['cap']:.2f}", "to": f"{after['cap']:.2f}",
              "level": after["level"]},
    )


def describe(ladder: Any, st: dict[str, Any]) -> str:
    steps = " → ".join(f"{s:.0f}" for s in st["steps"])
    nxt = ("top step reached" if st["top"]
           else f"{st['streak']} of {ladder.promote_after} confirmed orders towards the next step")
    return (f"autonomous cap {steps} EUR; one step up after {ladder.promote_after} confirmed "
            f"orders in a row; back to {st['steps'][0]:.0f} EUR after a rejected or declined "
            f"order. Now {st['cap']:.0f} EUR, {nxt}.")
