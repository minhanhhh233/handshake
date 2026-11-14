"""Settings from environment variables (document 02, section 7)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _load_dotenv() -> None:
    """Read a local .env for development. Existing environment variables win."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            v = v.replace("\\n", "\n")
            os.environ.setdefault(k, v)


@dataclass(frozen=True)
class Settings:
    database_url: str
    valio_api_base: str
    valio_api_key: str = field(repr=False)
    valio_customer: str
    signing_key_pem: str = field(repr=False)
    signing_kid: str
    public_base_url: str
    admin_key: str = field(repr=False)
    offer_ttl_seconds: int
    approval_ttl_seconds: int
    upstream_cache_ttl_seconds: int
    upstream_timeout_seconds: float
    demo_mode: bool


def load_settings() -> Settings:
    _load_dotenv()
    e = os.environ.get
    return Settings(
        database_url=e("DATABASE_URL", ""),
        valio_api_base=e("VALIO_API_BASE", "https://junction26.valioaimo.fi").rstrip("/"),
        valio_api_key=e("VALIO_API_KEY", ""),
        valio_customer=e("VALIO_CUSTOMER", "123"),
        signing_key_pem=e("SIGNING_KEY_PEM", ""),
        signing_kid=e("SIGNING_KID", "hs-2026-11"),
        public_base_url=e("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        admin_key=e("ADMIN_KEY", ""),
        offer_ttl_seconds=int(e("OFFER_TTL_SECONDS", "120")),
        approval_ttl_seconds=int(e("APPROVAL_TTL_SECONDS", "3600")),
        upstream_cache_ttl_seconds=int(e("UPSTREAM_CACHE_TTL_SECONDS", "60")),
        upstream_timeout_seconds=float(e("UPSTREAM_TIMEOUT_SECONDS", "5")),
        demo_mode=_bool(e("DEMO_MODE"), False),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


@lru_cache(maxsize=1)
def tls_context():  # noqa: ANN201
    """TLS context backed by the operating system's certificate store (verification on).

    Some development machines intercept TLS with a locally trusted CA that certifi lacks.
    """
    import ssl

    import truststore

    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
