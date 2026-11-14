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
    return page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("w,h,tag", [(390, 844, "390"), (1280, 800, "1280")])
def test_landing_and_example_order(browser, demo_server, w, h, tag):  # noqa: F811
    SCREENS.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=(w < 500),
                              has_touch=(w < 500), reduced_motion="reduce")
    page = ctx.new_page()
    page.goto(demo_server + "/")
    assert _no_hscroll(page)
    for name in ("Run the example order", "Run the allergy guard", "Search without a key"):
        b = page.get_by_role("button", name=name)
        assert b.is_visible() and b.bounding_box()["height"] >= 44
    page.screenshot(path=str(SCREENS / f"landing-{tag}.png"), full_page=True)

    page.get_by_role("button", name="Run the example order").click()
    page.wait_for_url("**/demo/runs/**")
    assert _no_hscroll(page)
    assert page.get_by_text("Your turn: approve this order").is_visible()
    page.screenshot(path=str(SCREENS / f"demo-run-{tag}.png"), full_page=True)
    approve = page.get_by_role("link", name="Open the approval page").get_attribute("href")
    run_url = page.url

    # The visitor approves on the approval page (same server; the link's host is PUBLIC_BASE_URL).
    page.goto(demo_server + urlparse(approve).path)
    page.get_by_role("button", name="Approve order").click()
    page.wait_for_load_state()
    page.goto(run_url)
    assert page.get_by_text("Approved. The order is confirmed.").is_visible()
    assert page.get_by_role("link", name="Open the signed receipt").is_visible()
    page.screenshot(path=str(SCREENS / f"demo-confirmed-{tag}.png"), full_page=True)
    ctx.close()
