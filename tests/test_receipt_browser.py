"""Receipt page in a real browser at phone and projector widths (task T-403).

Runs the app with uvicorn in a background thread against the test database. Skipped when
Playwright or its Chromium build is missing. Screenshots go to HANDSHAKE_SCREENS (default:
a `handshake-screens` folder in the system temp directory).
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from handshake import db, orders, upstream
from handshake.config import get_settings

from .factories import make_party
from .test_receipt_page import PID, delivery, fake_upstream

pytestmark = [pytest.mark.db, pytest.mark.browser]

sync_api = pytest.importorskip("playwright.sync_api")

SCREENS = Path(os.environ.get("HANDSHAKE_SCREENS",
                              Path(tempfile.gettempdir()) / "handshake-screens"))


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
    upstream.set_adapter(fake_upstream())
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


def _approved_order(base: str) -> tuple[str, str]:
    import httpx

    p = make_party()
    with httpx.Client(base_url=base, headers=p.headers, timeout=10) as c:
        r = c.post("/v1/offers", json={
            "delivery_date": delivery().isoformat(),
            "lines": [{"product_ids": [PID], "quantity": {"value": 1000, "unit": "L"}}]})
        assert r.status_code == 201, r.text
        r = c.post("/v1/orders", headers={"Idempotency-Key": "receipt-browser-1"}, json={
            "offer_id": r.json()["options"][0]["offer"]["offer_id"],
            "reference": "PO-2026-1114-07",
            "note": "Monday breakfast service. <script>alert(1)</script>"})
        assert r.status_code == 202, r.text
        order_id = r.json()["order_id"]
        with db.connection() as conn:
            a = orders.load_approval(conn, order_id)
        orders.decide_approval(orders.approval_token(a["approval_id"]), "approve",
                               "Fine for Monday.")
        r = c.get(f"/v1/orders/{order_id}/receipt")
        assert r.status_code == 200, r.text
        human = r.json()["human_url"]
    path = human[len(get_settings().public_base_url):]
    return order_id, path


def _open(page, url: str) -> None:
    page.goto(url)
    page.wait_for_load_state("networkidle", timeout=8000)


def _check(page) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.locator(".summary").is_visible()
    assert page.locator(".summary").inner_text().startswith("Ordered 1,000 L of")
    claim = page.locator(".integrity .claim")
    assert claim.is_visible() and "Record intact:" in claim.inner_text()
    seqs = [int(s) for s in page.locator(".chain .seq").all_inner_texts()]
    assert seqs == sorted(seqs) and len(seqs) >= 5
    texts = page.locator(".chain .node .text").all_inner_texts()
    assert any("approved the order" in t for t in texts)


def test_receipt_on_phones_and_projector(browser, server, catalog, clean_db):
    SCREENS.mkdir(parents=True, exist_ok=True)
    order_id, path = _approved_order(server)
    dialogs: list[str] = []
    requests: list[str] = []

    for w, h in ((390, 844), (360, 740)):
        ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=2,
                                  is_mobile=True, has_touch=True)
        page = ctx.new_page()
        page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        page.on("request", lambda r: requests.append(r.url))
        _open(page, server + path)
        _check(page)
        assert page.locator("text=Valio Aimo only").count() == 0
        page.screenshot(path=str(SCREENS / f"receipt-{w}.png"), full_page=True)
        ctx.close()

    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    page.on("request", lambda r: requests.append(r.url))
    _open(page, f"{server}/receipts/{order_id}?key={get_settings().admin_key}")
    assert page.url == f"{server}/receipts/{order_id}"  # key removed from the address bar
    assert page.locator(".tag", has_text="Valio Aimo only").count() >= 2
    assert page.locator(".snaps li").count() >= 1
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(SCREENS / "receipt-aimo-1280.png"), full_page=True)
    ctx.close()

    assert dialogs == []
    assert all(u.startswith(server) for u in requests), requests  # nothing external
