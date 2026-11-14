"""The live agent's offer-reading rules (document 07, section 8): a mismatch (the gateway
received a different quantity) versus ordinary partial availability (complete: false), and the
sentence about fewer options. Fake model: these tests check that each case gives the model the
fields the rules rely on, and that the rules are in its prompt."""

from __future__ import annotations

import itertools
import json

import pytest

from handshake import live_demo

from .test_claude_agent import FakeAnthropic, resp, text, tool
from .test_scenarios import world  # noqa: F401  (fixture re-export)

pytestmark = pytest.mark.db
_ips = itertools.count(1)


def go(sid, fake, behaviour="honest"):
    return live_demo.start("milk", f"10.120.0.{next(_ips)}", anthropic_client=fake, wait=True,
                           persona="hotel", sandbox_id=sid, behaviour=behaviour)


def offer_result(fake):
    """The request_offer result the model received (the message after its tool call)."""
    for req in fake.requests:
        last = req["messages"][-1]
        if isinstance(last["content"], list):
            for b in last["content"]:
                if b.get("type") == "tool_result":
                    body = json.loads(b["content"])
                    if "options" in body:
                        return body
    raise AssertionError("no offer result reached the model")


def test_partial_availability_is_not_a_mismatch(world):  # noqa: F811
    c, sid, d = world
    # 469363 has 384 units in the fake upstream: an honest, partial offer.
    fake = FakeAnthropic([
        resp(tool("request_offer", delivery_date=d.isoformat(), lines=[
            {"product_ids": ["469363"], "quantity": {"value": 1000, "unit": "L"}}])),
        resp(text("384 L can be delivered by the date; I have not ordered."), stop="end_turn")])
    go(sid, fake)
    f = offer_result(fake)["options"][0]["offer"]["fulfilment"][0]
    assert f["requested"]["value"] == 1000          # what the agent asked for: not a mismatch
    assert f["offered"]["value"] == 384 and f["complete"] is False
    system = fake.requests[0]["system"][0]["text"]
    assert "Partial availability" in system and "Mismatch" in system
    assert "only if the request said" in system and "change the date" in system


def test_over_orders_is_a_mismatch(world):  # noqa: F811
    c, sid, d = world
    fake = FakeAnthropic([
        resp(tool("request_offer", delivery_date=d.isoformat(), lines=[
            {"product_ids": ["464306"], "quantity": {"value": 100, "unit": "L"}}])),
        resp(text("The signed offer does not match my request."), stop="end_turn")])
    go(sid, fake, "over_orders")
    asked = fake.requests[0]["messages"]  # the model's own call is in the next request
    own_call = next(b for b in fake.requests[1]["messages"][-2]["content"]
                    if getattr(b, "type", None) == "tool_use" or
                    (isinstance(b, dict) and b.get("type") == "tool_use"))
    own_input = own_call.input if hasattr(own_call, "input") else own_call["input"]
    assert own_input["lines"][0]["quantity"]["value"] == 100 and asked
    f = offer_result(fake)["options"][0]["offer"]["fulfilment"][0]
    assert f["requested"]["value"] == 1000            # the gateway received 10 times more
    assert f["complete"] is True                      # complete, so not partial availability


def test_fewer_options_carry_the_reasons(world):  # noqa: F811
    c, sid, d = world
    fake = FakeAnthropic([
        resp(tool("request_offer", delivery_date=d.isoformat(), max_options=3, lines=[
            {"product_ids": ["464306", "465039", "469288"],
             "quantity": {"value": 100, "unit": "L"}}])),
        resp(text("One candidate was orderable; two were excluded."), stop="end_turn")])
    go(sid, fake)
    body = offer_result(fake)
    assert len(body["options"]) < 3
    reasons = {e["product_id"]: e["reason"] for e in body["excluded"]}
    assert reasons == {"465039": "not_deliverable_by_date", "469288": "out_of_stock"}
    assert "how many candidates were orderable" in fake.requests[0]["system"][0]["text"]


def test_no_forbidden_allergens_means_no_allergen_exclusions(world):  # noqa: F811
    """The Hotel chain's mandate lists no forbidden allergens. The model sees that in its
    mandate, and its prompt forbids citing rules the mandate lacks or excluding products on
    allergen grounds by itself."""
    c, sid, d = world
    fake = FakeAnthropic([
        resp(tool("get_mandate")),
        resp(text("Only 384 L of the 1,000 L you asked for can be delivered by 16 Nov, so I "
                  "have not ordered."), stop="end_turn")])
    go(sid, fake)
    mandate = json.loads(fake.requests[1]["messages"][-1]["content"][0]["content"])
    assert mandate["forbidden_allergens"]["codes"] == []
    system = fake.requests[0]["system"][0]["text"]
    assert "Never cite a mandate rule your mandate does not contain" in system
    assert "unless your mandate lists forbidden allergens or the request asks for" in system
    assert 'address the person who wrote the request as "you"' in system
    assert "Only 384 L of the 1,000 L you" in system
    for line in system.splitlines():
        assert "When the visitor" not in line
