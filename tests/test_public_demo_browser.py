"""Public landing page and guided demo in a real browser, at phone and desktop widths.

Same harness as test_pages_browser.py (uvicorn in a thread against the test database; skipped
without Playwright). Screenshots go to HANDSHAKE_SCREENS (default: %TEMP%/handshake-screens).
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlparse

import pytest

from handshake import db, public_demo, timeutil, upstream

from .fakes import FakeUpstream, make_facts
from .test_pages_browser import SCREENS, browser, sync_api  # noqa: F401 (fixtures)
from .test_pages_browser import _free_port

pytestmark = [pytest.mark.db, pytest.mark.browser]


@pytest.fixture
def demo_server(catalog, clean_db):
    import sys
    import threading
    import time
    from pathlib import Path

    import uvicorn

    from handshake.api import create_app

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import seed_demo

    with db.transaction() as conn:
        seed_demo.seed(conn)
    d = timeutil.today_helsinki() + timedelta(days=2)
    closing = timeutil.now() + timedelta(days=1)
    coffee = [("467004", "85.21", "14.20", "14"), ("468618", "67.18", "15.27", "92"),
              ("462245", "89.15", "14.86", "14"), ("464322", "104.52", "17.42", "585"),
              ("464724", "105.78", "17.22", "58")]
    upstream.set_adapter(FakeUpstream([
        make_facts(pid, price="1.01" if pid == "464306" else "1.60",
                   comparison="1.01" if pid == "464306" else "1.60", amount=None,
                   available_date=d, closing=closing) for pid in public_demo.SHORTLIST] + [
        make_facts(pid, price=pr, comparison=cmp, comparison_unit="KG", amount=amt,
                   available_date=d, closing=closing) for pid, pr, cmp, amt in coffee]))
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port,
                                        log_level="warning", access_log=False))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    assert srv.started
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=5)
    upstream.set_adapter(None)


def _no_hscroll(page) -> bool:
    # Compare with the requested viewport: in mobile emulation innerWidth grows to fit
    # overflowing content, which would hide the very overflow this checks for.
    return page.evaluate("document.documentElement.scrollWidth") <= page.viewport_size["width"]


@pytest.mark.parametrize("w,h,tag", [(390, 844, "390"), (1280, 800, "1280")])
def test_scenario_chip_verify_and_approver_inbox(browser, demo_server, w, h, tag):  # noqa: F811
    """Stage 2: a scripted scenario chip, the visitor's own signature check, the Approver
    inbox, then the confirmed order, the run page and the receipt."""
    SCREENS.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=(w < 500),
                              has_touch=(w < 500), reduced_motion="reduce")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(demo_server + "/")
    assert _no_hscroll(page)
    assert page.get_by_role("link", name="View API docs").is_visible()
    assert page.locator("#demo").count() == 0  # the three scripted buttons are replaced
    page.get_by_text("All customers and people shown are fictional.").is_visible()
    page.screenshot(path=str(SCREENS / f"landing-{tag}.png"), full_page=True)
    page.locator('.pb[data-persona="hotel"]').click()
    chip = page.locator('.chip[data-scenario="hotel_milk"]')
    chip.wait_for(timeout=10000)
    assert chip.bounding_box()["height"] >= 32
    chip.click()
    page.get_by_text("Your turn: approve this order").wait_for(timeout=20000)
    assert _no_hscroll(page)
    # The visitor checks the signature, then edits the price.
    page.get_by_role("button", name="Verify in this browser").click()
    page.locator(".vres.ok").wait_for(timeout=10000)
    assert "signature valid" in page.locator(".vres").inner_text()
    page.get_by_role("button", name="Edit the price, verify again").click()
    page.locator(".vres.bad").wait_for(timeout=10000)
    assert "INVALID" in page.locator(".vres").inner_text()
    page.locator("#live").screenshot(path=str(SCREENS / f"scenario-pending-{tag}.png"))
    # Approver role: the sandbox's inbox.
    page.locator('.pb[data-persona="approver"]').click()
    item = page.locator("#lv-inbox .item")
    item.wait_for(timeout=10000)
    assert "1010.00 EUR" in item.inner_text()
    page.locator("#lv-inbox").screenshot(path=str(SCREENS / f"approver-inbox-{tag}.png"))
    item.get_by_role("button", name="Approve").click()
    page.get_by_text("Nothing is waiting.").wait_for(timeout=10000)
    page.locator('.pb[data-persona="hotel"]').click()
    page.get_by_text("The order is confirmed.").wait_for(timeout=15000)
    receipt = page.get_by_role("link", name="Open the signed receipt").get_attribute("href")
    run_page = page.get_by_role("link", name="Open this run as a page").get_attribute("href")
    page.goto(demo_server + run_page)
    assert page.get_by_text("Approved. The order is confirmed.").is_visible()
    page.screenshot(path=str(SCREENS / f"demo-confirmed-{tag}.png"), full_page=True)
    page.goto(demo_server + urlparse(receipt).path + "?" + urlparse(receipt).query)
    assert _no_hscroll(page) and "Record intact" in page.inner_text("body")
    page.screenshot(path=str(SCREENS / f"receipt-{tag}.png"), full_page=True)
    assert not [e for e in errors if "Content Security Policy" in e or "Refused" in e], errors
    ctx.close()


@pytest.mark.parametrize("w,h,tag", [(390, 844, "390"), (1280, 800, "1280")])
def test_school_refusal_and_misbehaving_toggle(browser, demo_server, w, h, tag):  # noqa: F811
    SCREENS.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=(w < 500),
                              has_touch=(w < 500), reduced_motion="reduce")
    page = ctx.new_page()
    page.goto(demo_server + "/")
    page.locator('.pb[data-persona="school"]').click()
    page.locator('.chip[data-scenario="school_milk"]').click()
    page.get_by_text("The scenario finished.").wait_for(timeout=20000)
    assert "Lactose-free is not milk-free." in page.locator("#lv-steps").inner_text()
    page.locator("#live").screenshot(path=str(SCREENS / f"school-refused-{tag}.png"))
    page.check("#lv-bad")
    assert page.get_by_role("button", name="Run the misbehaving agent").is_visible()
    assert page.locator("#lv-prompt").is_disabled()
    page.get_by_role("button", name="Run the misbehaving agent").click()
    page.get_by_text("The scenario finished.").wait_for(timeout=30000)
    text = page.locator("#lv-steps").inner_text()
    for attempt in ("Attempt 1", "Attempt 3", "Attempt 5", "Attempt 6"):
        assert attempt in text
    assert _no_hscroll(page)
    page.locator("#live").screenshot(path=str(SCREENS / f"misbehaving-{tag}.png"))
    ctx.close()


@pytest.mark.parametrize("w,h,tag", [(390, 844, "390"), (1280, 800, "1280")])
def test_playground_try_it_live(browser, demo_server, w, h, tag):  # noqa: F811
    SCREENS.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=(w < 500),
                              has_touch=(w < 500), reduced_motion="reduce")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(demo_server + "/")
    cards = page.locator("[data-step]")
    assert cards.count() == 6

    def try_step(step: str) -> str:
        card = page.locator(f'[data-step="{step}"]')
        card.get_by_role("button", name="Try it live").click()
        state = card.locator(".state")
        state.filter(has_text="Done").wait_for(timeout=15000)
        assert card.locator(".res").inner_text().lstrip().startswith("{")
        return card.locator(".res").inner_text()

    try_step("search")
    assert '"jws"' in try_step("request_offer")
    assert '"verified": true' in try_step("verify_signature")
    assert "needs_approval" in try_step("dry_run")
    assert "pending_approval" in try_step("place_order")
    you = page.locator('[data-step="place_order"] .you a')
    assert you.is_visible()
    approve = you.get_attribute("href")
    assert "/approve/" in approve
    assert _no_hscroll(page)
    page.locator("#playground").screenshot(path=str(SCREENS / f"playground-{tag}.png"))

    # Copy button: clipboard may be unavailable headless; the label still confirms.
    page.locator('[data-copy="qs-1"]').click()
    page.locator('[data-copy="qs-1"][data-copied]').wait_for(timeout=3000)
    page.locator("#discovery").screenshot(path=str(SCREENS / f"discovery-{tag}.png"))
    page.locator("#quick-start").screenshot(path=str(SCREENS / f"quick-start-{tag}.png"))

    ap = ctx.new_page()
    ap.goto(demo_server + urlparse(approve).path)
    ap.get_by_role("button", name="Approve order").click()
    ap.wait_for_load_state()
    ap.close()
    assert "confirmed" in try_step("get_receipt")
    assert not [e for e in errors if "Content Security Policy" in e or "Refused" in e], errors
    ctx.close()


@pytest.mark.parametrize("w,h,tag", [(390, 844, "390"), (1280, 800, "1280")])
def test_live_agent_panels(browser, demo_server, monkeypatch, w, h, tag):  # noqa: F811
    from handshake import live_demo

    from .test_claude_agent import FakeAnthropic
    from .test_live_demo import milk_script

    d = (timeutil.today_helsinki() + timedelta(days=2)).isoformat()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(live_demo, "CLIENT_FACTORY", lambda: FakeAnthropic(milk_script(d)))
    SCREENS.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=(w < 500),
                              has_touch=(w < 500), reduced_motion="reduce")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(demo_server + "/")
    assert page.get_by_text("Handshake: the Valio Aimo interface").first.is_visible()
    assert page.get_by_text("not part of Handshake").first.is_visible()
    assert page.get_by_text("Live API").first.is_visible()
    # Persona bar and mandate card (stage 1).
    page.locator('.pb[data-persona="hotel"][aria-pressed="true"]').wait_for(timeout=10000)
    page.locator("#lv-card").get_by_text("Revontuli Hotels").wait_for(timeout=10000)
    assert _no_hscroll(page)
    page.locator('.pb[data-persona="restaurant"]').click()
    page.locator("#lv-card").get_by_text("Bistro Kallio").wait_for(timeout=10000)
    page.fill("#cap-input", "250")
    page.get_by_role("button", name="Save cap").click()
    page.locator("#lv-card").get_by_text("Saved as mandate version 2").wait_for(timeout=10000)
    page.screenshot(path=str(SCREENS / f"persona-card-{tag}.png"), full_page=False)
    page.locator('.pb[data-persona="hotel"]').click()
    page.locator("#lv-card").get_by_text("Revontuli Hotels").wait_for(timeout=10000)
    page.fill("#lv-prompt", "I need 1,000 litres of lactose-free milk for Monday. Choose on "
                            "price and availability.")
    page.get_by_role("button", name="Run the agent").click()
    page.get_by_text("Your turn: approve this order").wait_for(timeout=20000)
    assert page.locator(".lv-row.verify").count() == 1
    assert page.locator(".lv-row.call").count() == 5
    assert page.locator(".lv-card img").get_attribute("src").startswith("data:image/svg+xml")
    assert page.locator("#lv-timer .t-label").inner_text() == "Time to order"
    assert page.locator("#lv-timer-val").inner_text().endswith(" s")
    assert _no_hscroll(page)
    page.locator("#live").screenshot(path=str(SCREENS / f"live-pending-{tag}.png"))
    approve = page.locator(".lv-card a.btn").get_attribute("href")
    ap = ctx.new_page()
    ap.goto(demo_server + urlparse(approve).path)
    ap.get_by_role("button", name="Approve order").click()
    ap.wait_for_load_state()
    ap.close()
    page.get_by_text("The order is confirmed.").wait_for(timeout=15000)
    assert page.get_by_role("link", name="Open the signed receipt").is_visible()
    assert _no_hscroll(page)
    page.locator("#live").screenshot(path=str(SCREENS / f"live-confirmed-{tag}.png"))
    assert not [e for e in errors if "Content Security Policy" in e or "Refused" in e], errors
    ctx.close()


@pytest.mark.parametrize("w,h,tag", [(390, 844, "390"), (1280, 800, "1280")])
def test_start_here_strip(browser, demo_server, w, h, tag):  # noqa: F811
    SCREENS.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=(w < 500),
                              has_touch=(w < 500), reduced_motion="reduce")
    page = ctx.new_page()
    page.goto(demo_server + "/")
    steps = page.locator(".start-here button")
    assert steps.count() == 4
    for i in range(4):
        assert steps.nth(i).bounding_box()["height"] <= 60  # one line per step
    page.screenshot(path=str(SCREENS / f"start-here-{tag}.png"))
    steps.nth(3).click()  # verify before any run: a hint
    assert "Run step 1 first" in page.locator("#start-note").inner_text()
    steps.nth(0).click()
    page.get_by_text("Your turn: approve this order").wait_for(timeout=20000)
    steps.nth(3).click()
    page.locator(".vres.ok").wait_for(timeout=10000)
    steps.nth(2).click()
    page.locator("#lv-steps").get_by_text("Lactose-free is not milk-free.").first.wait_for(
        timeout=20000)
    steps.nth(1).click()
    page.get_by_text("Every attempt was refused").wait_for(timeout=30000)
    assert _no_hscroll(page)
    ctx.close()


@pytest.mark.parametrize("w,h,tag", [(390, 844, "390"), (1280, 800, "1280")])
def test_compare_offers_with_a_preference(browser, demo_server, w, h, tag):  # noqa: F811
    SCREENS.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=(w < 500),
                              has_touch=(w < 500), reduced_motion="reduce")
    page = ctx.new_page()
    page.goto(demo_server + "/")
    page.locator('.pb[data-persona="hotel"]').click()
    page.get_by_role("button", name="Finnish-origin or organic labels").click()
    page.locator("#lv-card").get_by_text("Ranking set").wait_for(timeout=10000)
    page.locator('.chip[data-scenario="compare_options"]').click()
    table = page.locator("table.cmp")
    table.wait_for(timeout=20000)
    text = table.inner_text()
    for row in ("Total excl. tax", "Products", "Labels", "Score", "best", "good"):
        assert row in text
    assert _no_hscroll(page)
    page.locator(".lv-row.compare").screenshot(path=str(SCREENS / f"compare-{tag}.png"))
    ctx.close()
