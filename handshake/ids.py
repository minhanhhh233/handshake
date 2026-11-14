"""Prefixed identifiers: prefix + 20 lowercase base32 characters (README conventions)."""

import re
import secrets

_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
PREFIXES = ("agt", "cus", "mnd", "off", "ord", "apr", "evt", "trc", "sbx")
ID_RE = re.compile(r"^(agt|cus|mnd|off|ord|apr|evt|trc|sbx)_[a-z2-7]{20}$")


def new_id(prefix: str) -> str:
    if prefix not in PREFIXES:
        raise ValueError(f"unknown id prefix {prefix!r}")
    return prefix + "_" + "".join(secrets.choice(_ALPHABET) for _ in range(20))


def is_id(value: str, prefix: str | None = None) -> bool:
    if not isinstance(value, str) or not ID_RE.match(value):
        return False
    return prefix is None or value.startswith(prefix + "_")


def new_token() -> str:
    """URL-safe secret token, 43 characters (256 bits). Used for approval and receipt tokens."""
    return secrets.token_urlsafe(32)
