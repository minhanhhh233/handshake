"""Create the Ed25519 signing key pair (task T-006; document 05, section 5).

Writes the private key PEM to a git-ignored file and prints the public JWK. The private key
goes into the Modal secret `handshake` as SIGNING_KEY_PEM. It is never printed unless asked.

    python scripts/gen_keys.py                     # writes keys/signing_key.pem
    python scripts/gen_keys.py --env-line          # also prints SIGNING_KEY_PEM=... for .env
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="keys/signing_key.pem")
    ap.add_argument("--kid", default=os.environ.get("SIGNING_KID", "hs-2026-11"))
    ap.add_argument("--force", action="store_true", help="overwrite an existing key file")
    ap.add_argument("--env-line", action="store_true",
                    help="print a SIGNING_KEY_PEM=... line (newlines escaped) for a local .env")
    args = ap.parse_args()

    if os.path.exists(args.out) and not args.force:
        print(f"{args.out} exists; use --force to replace it", file=sys.stderr)
        return 1

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "wb") as f:
        f.write(pem)

    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    jwk = {"kty": "OKP", "crv": "Ed25519", "kid": args.kid, "use": "sig", "alg": "EdDSA",
           "x": b64url(pub)}
    print(f"Private key written to {args.out} (git-ignored). Store it as SIGNING_KEY_PEM.")
    print("Public JWK:", json.dumps(jwk))
    if args.env_line:
        print("SIGNING_KEY_PEM=" + pem.decode().strip().replace("\n", "\\n"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
