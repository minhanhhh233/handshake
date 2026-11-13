"""Approval tokens and the admin key never reach the logs (document 08, section 0)."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from handshake.api import create_app, safe_path

TOKEN = "tok_SECRET_should_not_be_logged_0123456789abcdef"
ADMIN = "adminkey_SECRET_should_not_be_logged"


def test_safe_path_redacts_approval_tokens_only():
    assert safe_path(f"/approve/{TOKEN}") == "/approve/[redacted]"
    assert safe_path(f"/approve/{TOKEN}/done") == "/approve/[redacted]/done"
    assert safe_path("/v1/orders/ord_x") == "/v1/orders/ord_x"


@pytest.mark.db
def test_request_log_has_no_token_query_or_admin_key(clean_db, caplog):
    app = create_app()
    assert logging.getLogger("uvicorn.access").disabled
    client = TestClient(app)
    with caplog.at_level(logging.INFO):
        client.get(f"/approve/{TOKEN}?key={ADMIN}")
        client.get(f"/console?key={ADMIN}")
        client.get(f"/receipts/ord_x?t={TOKEN}")
    # Only server-side records: "httpx" is the test client's own request log (the caller's side).
    server = [r for r in caplog.records if not r.name.startswith("httpx")]
    assert any(r.name == "handshake" for r in server)
    text = "\n".join(r.getMessage() + " " + str(getattr(r, "path", "")) for r in server)
    assert "/approve/[redacted]" in text
    assert TOKEN not in text and ADMIN not in text
