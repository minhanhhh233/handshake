"""The two-page public site (/ and /live) in a real browser at phone and desktop widths
(document 09, section 6j). Scripted scenarios only: no model calls."""

from __future__ import annotations

import pytest

from .test_pages_browser import SCREENS, browser, sync_api  # noqa: F401 (fixtures)
from .test_public_demo_browser import _no_hscroll, demo_server  # noqa: F401 (fixture)

pytestmark = [pytest.mark.db, pytest.mark.browser]


def _ctx(browser, w, h):  # noqa: F811
    return browser.new_context(viewport={"width": w, "height": h}, is_mobile=(w < 500),
                               has_touch=(w < 500), reduced_motion="reduce")


@pytest.mark.parametrize("w,h,tag", [(390, 844, "390"), (1280, 800, "1280")])
def test_home_then_live_full_run_with_playback(browser, demo_server, w, h, tag):  # noqa: F811
    SCREENS.mkdir(parents=True, exist_ok=True)
    ctx = _ctx(browser, w, h)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    # Landing page.
    page.goto(demo_server + "/")
    assert _no_hscroll(page)
    assert page.get_by_role("heading", name="The wholesale interface built for AI purchasing "
                                               "agents").is_visible()
    buttons = page.get_by_role("link", name="See how it works in live action")
    assert buttons.count() == 2 and buttons.first.bounding_box()["height"] >= 44
    page.screenshot(path=str(SCREENS / f"home-{tag}.png"), full_page=True)

    # Live page: the hotel persona, the rules card and its edit controls.
    buttons.first.click()
    page.wait_for_url("**/live")
    page.locator('.pb[data-persona="hotel"]').click()
    page.locator("#rules-list li").first.wait_for(timeout=10000)
    assert page.locator("#rules-edit").is_visible()
    page.locator("#rules-edit").click()
    assert page.locator("#cap-input").is_visible()
    page.locator("#rules-edit").click()
    assert page.locator("#edit-box").is_hidden()
    assert _no_hscroll(page)

    # The scenario menu, grouped by theme.
    page.locator("#scen-toggle").click()
    for g in ("Ordering", "Limits and approval", "Exceptions", "Misbehaving agents",
              "Proactive offers"):
        assert page.locator("#scen-groups h3", has_text=g).count() == 1, g
    page.screenshot(path=str(SCREENS / f"live-start-{tag}.png"), full_page=True)

    # One full run: the hotel example order, which needs approval.
    page.locator("#scen-groups .scen", has_text="Example milk order").first.click()
    page.get_by_text("Your turn: approve this order").wait_for(timeout=20000)
    page.wait_for_function("() => !document.getElementById('lv-go').disabled", timeout=20000)
    assert page.locator("#lv-human .qr img").is_visible()
    gw = page.locator("#frame-gw .summary").inner_text()
    assert gw, "the gateway frame has a summary"
    total = int(page.locator("#pb-count").inner_text().split(" of ")[1])
    assert total >= 6
    assert page.locator("#pb-count").inner_text() == f"Step {total} of {total}"
    assert _no_hscroll(page)
    page.screenshot(path=str(SCREENS / f"live-pending-{tag}.png"), full_page=True)

    # Playback: back pauses following and shows Jump to latest; next; play; jump.
    page.locator("#pb-prev").click()
    page.locator("#pb-prev").click()
    assert page.locator("#pb-count").inner_text() == f"Step {total - 2} of {total}"
    assert page.locator("#pb-jump").is_visible()
    page.locator("#pb-next").click()
    assert page.locator("#pb-count").inner_text() == f"Step {total - 1} of {total}"
    # The signed offer appears in the gateway frame with a plain sentence above the JSON.
    while page.locator("#pb-prev").is_enabled():
        page.locator("#pb-prev").click()
    assert page.locator("#pb-count").inner_text().startswith("Step 1 of")
    page.locator("#pb-speed").select_option("4")
    page.locator("#pb-play").click()
    page.wait_for_function(
        "() => document.getElementById('frame-gw').innerText.indexOf('Signed offer:') >= 0",
        timeout=15000)
    assert "valid" in page.locator("#frame-gw .summary").inner_text()
    page.screenshot(path=str(SCREENS / f"live-playback-{tag}.png"), full_page=False)
    page.locator("#pb-play").click()  # pause
    page.locator("#pb-jump").click()
    assert page.locator("#pb-count").inner_text() == f"Step {total} of {total}"
    assert page.locator("#pb-jump").is_hidden()
    # Show all steps: the full log; a click jumps to that step.
    page.locator("#pb-all").click()
    assert page.locator("#log li").count() == total
    page.locator("#log li button.head").nth(1).click()
    assert page.locator("#pb-count").inner_text() == f"Step 2 of {total}"
    page.locator("#log").screenshot(path=str(SCREENS / f"live-log-{tag}.png"))
    page.locator("#pb-all").click()

    # The Approver approves; back as the hotel, the outcome and receipt link appear.
    page.locator('.pb[data-persona="approver"]').click()
    item = page.locator("#role-panel .item")
    item.wait_for(timeout=10000)
    assert page.locator("#lv-form").is_hidden()
    item.get_by_role("button", name="Approve").click()
    page.get_by_text("Nothing is waiting.").wait_for(timeout=10000)
    page.locator('.pb[data-persona="hotel"]').click()
    page.get_by_text("The order is confirmed").wait_for(timeout=15000)
    assert page.get_by_role("link", name="Open the signed receipt").is_visible()
    page.screenshot(path=str(SCREENS / f"live-confirmed-{tag}.png"), full_page=True)

    # Operations: the agents and the order.
    page.locator('.pb[data-persona="operations"]').click()
    page.get_by_role("button", name="Tamper with the log").first.wait_for(timeout=10000)
    page.locator("#role-panel").screenshot(path=str(SCREENS / f"live-ops-{tag}.png"))
    assert _no_hscroll(page)
    assert not errors, errors
    ctx.close()


def test_start_links_and_misbehaving(browser, demo_server):  # noqa: F811
    ctx = _ctx(browser, 1280, 800)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    # Verify a signature yourself: the example runs and the check runs in the browser.
    page.goto(demo_server + "/live?start=verify")
    page.locator(".vres.ok").wait_for(timeout=25000)
    page.wait_for_function("() => !document.getElementById('lv-go').disabled", timeout=20000)
    # The scripted misbehaving sequence from the menu, as the restaurant.
    page.locator('.pb[data-persona="restaurant"]').click()
    page.locator("#scen-groups .scen.scripted").first.wait_for(state="attached", timeout=10000)
    page.locator("#scen-toggle").click()
    sec = page.locator("#scen-groups > div", has=page.locator("h3", has_text="Misbehaving agents"))
    sec.locator(".scen.scripted").first.click()
    page.wait_for_function("() => document.getElementById('lv-status').innerText.length > 0 && "
                           "!document.getElementById('lv-go').disabled", timeout=30000)
    page.locator("#pb-all").click()
    assert page.locator("#log li .sum", has_text="Refused").count() >= 1
    page.locator("#pb-all").click()
    page.screenshot(path=str(SCREENS / "live-misbehaving-1280.png"), full_page=True)
    # The school refuses milk.
    page.goto(demo_server + "/live?start=school")
    page.wait_for_function("() => document.getElementById('pb-count').innerText.indexOf('of') > 0 && "
                           "!document.getElementById('lv-go').disabled", timeout=30000)
    assert not errors, errors
    ctx.close()
