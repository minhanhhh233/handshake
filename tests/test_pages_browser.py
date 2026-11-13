"""Approval page and console in a real browser at phone and projector widths (T-302, T-303).

Runs the app with uvicorn in a background thread against the test database. Skipped when
Playwright or its Chromium build is missing. Screenshots go to HANDSHAKE_SCREENS (default:
a `screens` folder in the system temp directory).
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from handshake import db, orders, timeutil, upstream
from handshake.config import get_settings

from .factories import make_party
from .fakes import FakeUpstream, make_facts

pytestmark = [pytest.mark.db, pytest.mark.browser]

sync_api = pytest.importorskip("playwright.sync_api")

PID = "464306"
SCREENS = Path(os.environ.get("HANDSHAKE_SCREENS",
                              Path(tempfile.gettempdir()) / "handshake-screens"))


def delivery():
    d = timeutil.today_helsinki() + timedelta(days=2)
    while d.isoweekday() == 7:
        d += timedelta(days=1)
    return d


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def browser():
    try:
        pw = sync_api.sync_playwright().start()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Playwright unavailable: {e}")
    try:
        b = pw.chromium.launch(headless=True)
    except Exception as e:  # noqa: BLE001
        pw.stop()
        pytest.skip(f"Chromium for Playwright not installed: {e}")
    yield b
    b.close()
    pw.stop()


@pytest.fixture
def server(test_engine):
    import uvicorn

    from handshake.api import create_app

    if not get_settings().admin_key:
        pytest.skip("ADMIN_KEY not set")
    upstream.set_adapter(FakeUpstream([make_facts(
        PID, price="1.01", comparison="1.01", amount=None, available_date=delivery(),
        closing=timeutil.now() + timedelta(days=1))]))
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port,
                                        log_level="warning", access_log=False))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    assert srv.started, "uvicorn did not start"
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=5)
    upstream.set_adapter(None)


def _pending_order(base: str) -> str:
    import httpx

    p = make_party()
    with httpx.Client(base_url=base, headers=p.headers, timeout=10) as c:
        r = c.post("/v1/offers", json={
            "delivery_date": delivery().isoformat(),
            "lines": [{"product_ids": [PID], "quantity": {"value": 1000, "unit": "L"}}]})
        assert r.status_code == 201, r.text
        r = c.post("/v1/orders", headers={"Idempotency-Key": "browser-key-1"}, json={
            "offer_id": r.json()["options"][0]["offer"]["offer_id"], "reference": "PO-2026-1114-07",
            "note": "Monday breakfast service. <script>alert(1)</script>"})
        assert r.status_code == 202, r.text
    return r.json()["order_id"]


def _token(order_id: str) -> str:
    with db.connection() as conn:
        return orders.approval_token(orders.load_approval(conn, order_id)["approval_id"])


def _status(order_id: str) -> str:
    with db.connection() as conn:
        return orders.load_order(conn, order_id)["status"]


def _open(page, url: str) -> None:
    page.goto(url)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:  # noqa: BLE001 - web fonts unreachable: the fallback font is fine
        pass


def _check_layout(page) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.locator(".bar").is_visible()
    for name in ("Approve order", "Decline order"):
        btn = page.get_by_role("button", name=name)
        btn.scroll_into_view_if_needed()
        assert btn.is_visible()
        assert btn.bounding_box()["height"] >= 44


def test_approval_page_on_phones(browser, server, catalog, clean_db):
    SCREENS.mkdir(parents=True, exist_ok=True)
    order_id = _pending_order(server)
    url = f"{server}/approve/{_token(order_id)}"
    dialogs: list[str] = []

    ctx = browser.new_context(viewport={"width": 360, "height": 740}, device_scale_factor=2,
                              is_mobile=True, has_touch=True)
    page = ctx.new_page()
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    _open(page, url)
    sentence = page.locator(".meter-sentence").inner_text().replace("\xa0", " ")
    assert sentence == "1010.00 EUR is 10.00 over your 1000.00 EUR limit."
    _check_layout(page)
    page.screenshot(path=str(SCREENS / "approval-360.png"), full_page=True)
    ctx.close()

    ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                              is_mobile=True, has_touch=True)
    page = ctx.new_page()
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    _open(page, url)
    _check_layout(page)
    page.screenshot(path=str(SCREENS / "approval-390.png"), full_page=True)
    page.get_by_role("button", name="Approve order").tap()
    page.wait_for_url("**/done")
    assert "Approved. Your agent will see the order as confirmed." in page.content()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(SCREENS / "result-390.png"), full_page=True)
    ctx.close()

    assert dialogs == []  # the agent's note never ran as script
    assert _status(order_id) == "confirmed"


def test_console_on_projector(browser, server, catalog, clean_db, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    get_settings.cache_clear()
    try:
        SCREENS.mkdir(parents=True, exist_ok=True)
        _pending_order(server)
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        _open(page, f"{server}/console?key={get_settings().admin_key}")
        assert page.url == f"{server}/console"  # key removed from the address bar
        assert page.locator("svg.qr").count() == 1
        assert page.get_by_role("link", name="Open approval page").is_visible()
        page.screenshot(path=str(SCREENS / "console-1280.png"), full_page=False)
        # live refresh keeps working
        page.wait_for_timeout(3500)
        assert "refreshes every 3 seconds" in page.locator("#status").inner_text()
        ctx.close()
    finally:
        get_settings.cache_clear()
