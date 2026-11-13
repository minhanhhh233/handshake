"""Ed25519 JWS signing for offers and receipts (document 05, section 5; task T-107).

Payload: the object as UTF-8 JSON, keys sorted, no insignificant whitespace.
Header: {"alg": "EdDSA", "kid": SIGNING_KID, "typ": "handshake-offer+jwt" | "handshake-receipt+jwt"}.
"""

from __future__ import annotations

import base64
import json
from functools import lru_cache
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jwt.algorithms import OKPAlgorithm

from .config import get_settings

TYP_OFFER = "handshake-offer+jwt"
TYP_RECEIPT = "handshake-receipt+jwt"
ALG = "EdDSA"


class SignatureError(Exception):
    """Verification failed: bad signature, wrong algorithm, unknown kid or wrong typ."""


def canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@lru_cache(maxsize=1)
def _private_key() -> Ed25519PrivateKey:
    pem = get_settings().signing_key_pem.replace("\\n", "\n").strip()
    if not pem:
        raise RuntimeError("SIGNING_KEY_PEM is not set")
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError("SIGNING_KEY_PEM is not an Ed25519 key")
    return key


def reset_cache() -> None:
    _private_key.cache_clear()


def sign(payload: dict[str, Any], typ: str) -> str:
    """Compact JWS over the canonical JSON of `payload`."""
    return jwt.PyJWS().encode(
        canonical(payload), _private_key(), algorithm=ALG,
        headers={"kid": get_settings().signing_kid, "typ": typ},
    )


def public_jwk() -> dict[str, str]:
    raw = _private_key().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {
        "kty": "OKP", "crv": "Ed25519", "kid": get_settings().signing_kid, "use": "sig",
        "alg": ALG, "x": base64.urlsafe_b64encode(raw).rstrip(b"=").decode(),
    }


def jwks() -> dict[str, list[dict[str, str]]]:
    return {"keys": [public_jwk()]}


def verify(token: str, key_set: dict[str, Any], expected_typ: str | None = None) -> dict[str, Any]:
    """Verify as an agent would (document 05, section 5.3, steps 2 to 4). Returns the payload."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise SignatureError(f"malformed JWS: {e}") from e
    if header.get("alg") != ALG:
        raise SignatureError(f"algorithm {header.get('alg')!r} refused; only EdDSA is accepted")
    if expected_typ and header.get("typ") != expected_typ:
        raise SignatureError(f"typ {header.get('typ')!r}, expected {expected_typ!r}")
    jwk = next((k for k in key_set.get("keys", []) if k.get("kid") == header.get("kid")), None)
    if jwk is None:
        raise SignatureError(f"no key with kid {header.get('kid')!r}")
    key = OKPAlgorithm.from_jwk(json.dumps(jwk))
    try:
        payload = jwt.PyJWS().decode(token, key, algorithms=[ALG])
    except jwt.PyJWTError as e:
        raise SignatureError(f"signature invalid: {e}") from e
    return json.loads(payload)
