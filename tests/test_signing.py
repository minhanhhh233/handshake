"""Signing, discovery and key set (document 09, section 2.4: S-01 to S-03; document 03, section 2)."""

from __future__ import annotations

import base64
import json

import jwt
import pytest
from fastapi.testclient import TestClient

from handshake import signing
from handshake.api import create_app
from handshake.config import get_settings


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app())


def _offer() -> dict:
    return {
        "offer_id": "off_aaaaaaaaaaaaaaaaaaaa",
        "issuer": get_settings().public_base_url,
        "agent_id": "agt_aaaaaaaaaaaaaaaaaaaa",
        "expires_at": "2099-01-01T00:00:00+02:00",
        "currency": "EUR",
        "lines": [{"line_no": 1, "product_id": "464398", "unit_price_excl_tax": "1.05"}],
        "totals": {"excl_tax": "1.05", "tax": "0.14", "incl_tax": "1.19"},
    }


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def test_discovery_document(client: TestClient):
    s = get_settings()
    r = client.get("/.well-known/handshake.json")
    assert r.status_code == 200
    d = r.json()
    base = s.public_base_url
    assert d["api_base"] == f"{base}/v1"
    assert d["openapi"] == f"{base}/openapi.json"
    assert d["jwks_uri"] == f"{base}/.well-known/jwks.json"
    assert d["capabilities"] == ["product_search", "signed_offers", "dry_run", "orders",
                                 "human_approval", "receipts"]
    assert d["signing"] == {"alg": "EdDSA", "format": "JWS compact",
                            "typ_offer": "handshake-offer+jwt",
                            "typ_receipt": "handshake-receipt+jwt"}
    assert d["offer_ttl_seconds"] == s.offer_ttl_seconds
    assert d["pricing_model"] == "fixed"
    assert d["authentication"]["anonymous"] == ["product_search"]
    assert d["authentication"]["agent"] == "bearer" and "Register" in d["authentication"]["registration"]
    assert d["currency"] == "EUR" and d["time_zone"] == "Europe/Helsinki"
    assert d["version"] == "1.0" and d["name"] and d["data_notice"]
    assert r.headers["X-Trace-Id"].startswith("trc_")


def test_jwks_shape(client: TestClient):
    r = client.get("/.well-known/jwks.json")
    assert r.status_code == 200
    keys = r.json()["keys"]
    assert len(keys) == 1
    k = keys[0]
    assert k["kty"] == "OKP" and k["crv"] == "Ed25519" and k["alg"] == "EdDSA" and k["use"] == "sig"
    assert k["kid"] == get_settings().signing_kid
    assert len(_b64d(k["x"])) == 32
    assert "d" not in k  # never the private part


def test_s01_signed_offer_verifies_with_published_key(client: TestClient):
    offer = _offer()
    token = signing.sign(offer, signing.TYP_OFFER)
    header = jwt.get_unverified_header(token)
    assert header == {"alg": "EdDSA", "kid": get_settings().signing_kid,
                      "typ": "handshake-offer+jwt"}
    published = client.get("/.well-known/jwks.json").json()
    assert signing.verify(token, published, expected_typ=signing.TYP_OFFER) == offer
    # The payload is the canonical JSON: sorted keys, no whitespace.
    assert _b64d(token.split(".")[1]) == signing.canonical(offer)


def test_s02_one_character_change_fails(client: TestClient):
    token = signing.sign(_offer(), signing.TYP_OFFER)
    h, p, sig = token.split(".")
    raw = _b64d(p).decode()
    assert '"unit_price_excl_tax":"1.05"' in raw
    tampered = f"{h}.{_b64e(raw.replace('1.05', '1.04', 1).encode())}.{sig}"
    published = client.get("/.well-known/jwks.json").json()
    with pytest.raises(signing.SignatureError):
        signing.verify(tampered, published)


def test_s03_non_eddsa_algorithm_refused(client: TestClient):
    published = client.get("/.well-known/jwks.json").json()
    kid = get_settings().signing_kid
    hs = jwt.encode(_offer(), "attacker-secret-attacker-secret-32b", algorithm="HS256",
                    headers={"kid": kid, "typ": signing.TYP_OFFER})
    with pytest.raises(signing.SignatureError, match="only EdDSA"):
        signing.verify(hs, published)
    # alg "none" too
    none = jwt.encode(_offer(), None, algorithm="none", headers={"kid": kid})
    with pytest.raises(signing.SignatureError):
        signing.verify(none, published)


def test_wrong_typ_and_unknown_kid_refused(client: TestClient):
    published = client.get("/.well-known/jwks.json").json()
    receipt = signing.sign({"x": 1}, signing.TYP_RECEIPT)
    with pytest.raises(signing.SignatureError, match="typ"):
        signing.verify(receipt, published, expected_typ=signing.TYP_OFFER)
    other = {"keys": [{**published["keys"][0], "kid": "someone-else"}]}
    with pytest.raises(signing.SignatureError, match="kid"):
        signing.verify(receipt, other)


def test_discovery_needs_no_authentication(client: TestClient):
    r = client.get("/.well-known/handshake.json", headers={"Authorization": "Bearer nonsense"})
    assert r.status_code == 200
    assert json.loads(r.text)["jwks_uri"].endswith("/.well-known/jwks.json")
