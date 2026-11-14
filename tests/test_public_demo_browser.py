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
    upstream.set_adapter(FakeUpstream([
        make_facts(pid, price="1.01" if pid == "464306" else "1.60",
                   comparison="1.01" if pid == "464306" else "1.60", amount=None,
                   available_date=d, closing=closing) for pid in public_demo.SHORTLIST]))
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
def test_landing_and_example_order(browser, demo_server, w, h, tag):  # noqa: F811
    SCREENS.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=(w < 500),
                              has_touch=(w < 500), reduced_motion="reduce")
    page = ctx.new_page()
    page.goto(demo_server + "/")
    assert _no_hscroll(page)
    assert page.get_by_role("link", name="View API docs").is_visible()
    for name in ("Run the example order", "Run the allergy guard", "Search without a key"):
        for b in page.get_by_role("button", name=name).all():
            b.scroll_into_view_if_needed()
            assert b.is_visible() and b.bounding_box()["height"] >= 44
    page.get_by_text("Prototype built for the Valio Aimo challenge at Junction 2026").is_visible()
    page.screenshot(path=str(SCREENS / f"landing-{tag}.png"), full_page=True)

    page.locator(".tile.primary button").click()
    page.wait_for_url("**/demo/runs/**")
    assert _no_hscroll(page)
    assert page.get_by_text("Your turn: approve this order").is_visible()
    page.get_by_text("Request / response").first.click()  # expandable block per step
    assert page.locator("details.io[open] pre").first.is_visible()
    assert _no_hscroll(page)
    page.screenshot(path=str(SCREENS / f"demo-run-{tag}.png"), full_page=True)
    approve = page.get_by_role("link", name="Open the approval page").get_attribute("href")
    run_url = page.url

    # The visitor approves on the approval page (same server; the link's host is PUBLIC_BASE_URL).
    page.goto(demo_server + urlparse(approve).path)
    assert _no_hscroll(page)
    page.screenshot(path=str(SCREENS / f"approval-{tag}.png"), full_page=True)
    page.get_by_role("button", name="Approve order").click()
    page.wait_for_load_state()
    page.goto(run_url)
    assert page.get_by_text("Approved. The order is confirmed.").is_visible()
    assert page.get_by_role("link", name="Open the signed receipt").is_visible()
    page.screenshot(path=str(SCREENS / f"demo-confirmed-{tag}.png"), full_page=True)
    receipt = page.get_by_role("link", name="Open the signed receipt").get_attribute("href")
    page.goto(demo_server + urlparse(receipt).path + "?" + urlparse(receipt).query)
    assert _no_hscroll(page) and "Record intact" in page.inner_text("body")
    page.screenshot(path=str(SCREENS / f"receipt-{tag}.png"), full_page=True)
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
    page.locator(".chip").nth(1).click()
    assert "lactose-free" in page.locator("#lv-prompt").input_value()
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
