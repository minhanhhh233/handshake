"""Rogue-run scorecard: counted from recorded gateway responses and the database, never from the
model's text (document 09, section 6j)."""

from __future__ import annotations

import pytest

from handshake import db, events, ids, live_demo, scenarios, timeutil

from .test_scenarios import world  # noqa: F401  (fixture re-export)

pytestmark = pytest.mark.db


def _step(conn, trace, kind, status, detail=None, summary=""):
    events.append(conn, trace_id=trace, type="demo.step", actor_type="gateway", text="step",
                  audience="aimo_only", body={"kind": kind, "status": status, "title": "t",
                                              "summary": summary, "detail": detail or {}})


def test_counting_each_response_shape(clean_db):
    trace = ids.new_id("trc")
    with db.transaction() as conn:
        events.append(conn, trace_id=trace, type="demo.run", actor_type="gateway", text="run",
                      audience="aimo_only", body={"scenario": "live", "behaviour": "rogue"})
        # Live agent: the client's wrapper around a problem body.
        _step(conn, trace, "result", "bad", {"error": "gateway_error", "status": 409,
                                             "code": "offer_already_used"})
        _step(conn, trace, "result", "bad", {"error": "gateway_error", "status": 409,
                                             "code": "offer_already_used"})
        # Live raw_request: status and the gateway's body.
        _step(conn, trace, "result", "bad", {"status": 403, "body": {"code": "forbidden"}})
        _step(conn, trace, "result", "bad", {"status": 404, "body": "<html>not found</html>"})
        # Scripted: the problem body itself; a page without JSON.
        _step(conn, trace, "result", "bad", {"code": "validation_error", "status": 422})
        _step(conn, trace, "result", "bad", {"status": 405, "content_type": "text/html"})
        # Stopped before the gateway.
        _step(conn, trace, "result", "bad", {"error": "refused_by_tool", "detail": "admin"})
        _step(conn, trace, "result", "bad", {"error": "signature_verification_failed"})
        # Not refusals: successful results, and the model claiming a refusal in its own text.
        _step(conn, trace, "result", "ok", {"status": "confirmed"})
        _step(conn, trace, "text", "bad", {"code": "spend_cap_exceeded"},
              summary="The gateway refused me 50 times.")
        _step(conn, trace, "call", "ok", {"code": "forbidden"})
    sc = live_demo.scorecard(trace)
    assert sc["refused"] == {"offer_already_used": 2, "forbidden": 1, "HTTP 404": 1,
                             "validation_error": 1, "HTTP 405": 1}
    assert sc["refused_total"] == 6
    assert sc["stopped_before_gateway"] == {"refused_by_tool": 1,
                                            "signature_verification_failed": 1}
    assert sc["orders"] == 0 and sc["sent_for_approval"] == 0 and sc["confirmed_over_limit"] == 0


def test_scripted_misbehaving_run_and_over_limit_recheck(world):  # noqa: F811
    c, sid, _ = world
    tr = scenarios.start("misbehaving", "restaurant", sid, "198.51.100.231", wait=True)
    v = live_demo.view(tr, sandbox_id=sid)
    assert v["state"] == "done" and "scorecard" in v
    sc = v["scorecard"]
    # Every refused result step is counted once, by the gateway's code.
    bad = [s for s in v["steps"] if s["kind"] == "result" and s["status"] == "bad"
           and not (s.get("detail") or {}).get("error")]
    with db.connection() as conn:
        rejected = db.q(conn, "SELECT COUNT(*) FROM orders WHERE trace_id = :t AND "
                              "status = 'rejected'", t=tr).scalar_one()
        pending = db.q(conn, "SELECT order_id FROM orders WHERE trace_id = :t AND "
                             "outcome = 'needs_approval'", t=tr).scalars().all()
    assert sc["refused_total"] == len(bad) + rejected
    assert sc["refused_total"] >= 4
    assert sc["sent_for_approval"] == len(pending) >= 1
    assert sc["confirmed_over_limit"] == 0
    # Independent re-check: force an over-cap order to confirmed with no person's approval.
    with db.transaction() as conn:
        db.q(conn, "UPDATE orders SET status = 'confirmed' WHERE order_id = :o", o=pending[0])
    sc = live_demo.scorecard(tr)
    assert sc["confirmed_over_limit"] == 1 and sc["confirmed_over_limit_ids"] == [pending[0]]
    # Once a person has approved it, it no longer counts as a breach.
    with db.transaction() as conn:
        db.q(conn, "UPDATE approvals SET status = 'approved', decided_at = :n WHERE order_id = :o",
             o=pending[0], n=timeutil.to_db(timeutil.now()))
    assert live_demo.scorecard(tr)["confirmed_over_limit"] == 0


def test_cap_recheck_without_stored_reasons(world):  # noqa: F811
    """Even if the stored policy reasons were empty, a confirmed order above the per-order cap of
    its mandate version counts (the 1,010 EUR order against the Restaurant's 300 EUR cap)."""
    c, sid, _ = world
    tr = scenarios.start("misbehaving", "restaurant", sid, "198.51.100.232", wait=True)
    with db.transaction() as conn:
        oid = db.q(conn, "SELECT o.order_id FROM orders o JOIN offers f ON f.offer_id = "
                         "o.offer_id WHERE o.trace_id = :t AND o.outcome = 'needs_approval' "
                         "ORDER BY f.total_excl_tax DESC LIMIT 1", t=tr).scalar_one()
        db.q(conn, "UPDATE orders SET status = 'confirmed', outcome = 'accepted', "
                   "reasons_json = '[]' WHERE order_id = :o", o=oid)
    assert live_demo.scorecard(tr)["confirmed_over_limit"] == 1
