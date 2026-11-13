"""Verify a signed Handshake offer, then show that a one-character price change breaks it.

Demo step 4 (document 07, section 3):

    python agent/verify_offer.py --base-url http://127.0.0.1:8000 --jws <TOKEN>
    python agent/verify_offer.py --base-url http://127.0.0.1:8000 --jws-file offer.jws

Exit code 0 when the genuine offer verifies and the tampered copy is refused.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

try:  # imported as part of the `agent` package
    from .handshake_client import SignatureVerificationError, jws_kid, verify_jws
except ImportError:  # run as a script: python agent/verify_offer.py
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from handshake_client import SignatureVerificationError, jws_kid, verify_jws  # type: ignore[no-redef]


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _bump_digit(price: str) -> str:
    """Change exactly one character: the last digit, to a different digit."""
    for i in range(len(price) - 1, -1, -1):
        c = price[i]
        if c.isdigit():
            return price[:i] + str((int(c) + 1) % 10) + price[i + 1:]
    return price + "1"


def tamper(jws: str) -> tuple[str, str, str]:
    """Copy of `jws` with one character of the first line's unit_price_excl_tax changed.

    Header and signature are kept. Returns (tampered_jws, old_price, new_price).
    """
    header, payload_b64, signature = jws.split(".")
    raw = _b64d(payload_b64).decode("utf-8")
    payload = json.loads(raw)
    old = str(payload["lines"][0]["unit_price_excl_tax"])
    new = _bump_digit(old)
    needle = f'"unit_price_excl_tax":"{old}"'
    if needle in raw:  # edit the bytes in place so nothing else changes
        raw = raw.replace(needle, f'"unit_price_excl_tax":"{new}"', 1)
    else:
        payload["lines"][0]["unit_price_excl_tax"] = new
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{header}.{_b64e(raw.encode('utf-8'))}.{signature}", old, new


def fetch_jwks(base_url: str, http: httpx.Client) -> dict:
    base_url = base_url.rstrip("/")
    disc = http.get(f"{base_url}/.well-known/handshake.json")
    disc.raise_for_status()
    path = urlsplit(disc.json().get("jwks_uri") or "/.well-known/jwks.json").path
    resp = http.get(f"{base_url}{path or '/.well-known/jwks.json'}")
    resp.raise_for_status()
    return resp.json()


def run(base_url: str, jws: str, http: httpx.Client | None = None, out=print) -> bool:
    """Verify the genuine JWS and a tampered copy. True when genuine passes and tampered fails."""
    own = http is None
    http = http or httpx.Client(timeout=15.0)
    try:
        jwks = fetch_jwks(base_url, http)
    finally:
        if own:
            http.close()
    jws = jws.strip()

    try:
        offer = verify_jws(jws, jwks)
    except SignatureVerificationError as e:
        out(f"Signature invalid: {e}")
        return False
    totals = offer.get("totals") or {}
    out("Signature valid")
    out(f"  key:     {jws_kid(jws)} (EdDSA)")
    out(f"  offer:   {offer.get('offer_id')}")
    out(f"  total:   {totals.get('excl_tax')} {offer.get('currency', 'EUR')} excl. tax")
    out(f"  expires: {offer.get('expires_at')}")

    try:
        bad, old, new = tamper(jws)
    except (KeyError, IndexError, ValueError) as e:
        out(f"Cannot build tampered copy: {e}")
        return False
    out("")
    out(f"Tampered copy: line 1 unit_price_excl_tax {old} -> {new}")
    try:
        verify_jws(bad, jwks)
    except SignatureVerificationError as e:
        out(f"Signature invalid: {e}")
        return True
    out("Tampered copy VERIFIED - this must not happen")
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", required=True, help="Gateway base URL, e.g. http://127.0.0.1:8000")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--jws", help="Compact JWS of an offer")
    src.add_argument("--jws-file", help="File containing the compact JWS")
    args = ap.parse_args(argv)
    jws = args.jws if args.jws else Path(args.jws_file).read_text(encoding="utf-8")
    try:
        return 0 if run(args.base_url, jws) else 1
    except httpx.HTTPError as e:
        print(f"Cannot fetch the key set: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
