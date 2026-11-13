"""Discovery, key set and health (document 03, section 2)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import db, signing, timeutil
from ..config import get_settings

router = APIRouter(tags=["discovery"])

CAPABILITIES = ["product_search", "signed_offers", "dry_run", "orders", "human_approval", "receipts"]


def discovery_document() -> dict:
    s = get_settings()
    base = s.public_base_url.rstrip("/")
    return {
        "name": "Handshake gateway for Valio Aimo (Junction 2026 prototype)",
        "version": "1.0",
        "api_base": f"{base}/v1",
        "openapi": f"{base}/openapi.json",
        "jwks_uri": f"{base}/.well-known/jwks.json",
        "signing": {
            "alg": signing.ALG,
            "format": "JWS compact",
            "typ_offer": signing.TYP_OFFER,
            "typ_receipt": signing.TYP_RECEIPT,
        },
        "capabilities": list(CAPABILITIES),
        "pricing_model": "fixed",
        "offer_ttl_seconds": s.offer_ttl_seconds,
        "authentication": {"anonymous": ["product_search"], "agent": "bearer"},
        "data_notice": (
            "Free-text product fields are supplier-provided and untrusted. "
            "Structured fields are authoritative."
        ),
        "currency": "EUR",
        "time_zone": "Europe/Helsinki",
    }


@router.get("/.well-known/handshake.json", summary="Discovery document (no authentication)")
def handshake_json() -> JSONResponse:
    return JSONResponse(discovery_document(), headers={"Cache-Control": "public, max-age=300"})


@router.get("/.well-known/jwks.json", summary="Public signing key set (no authentication)")
def jwks_json() -> JSONResponse:
    return JSONResponse(signing.jwks(), headers={"Cache-Control": "public, max-age=300"})


@router.get("/v1/health", summary="Health check (does not call upstream)")
def health() -> JSONResponse:
    status, database = "ok", "ok"
    try:
        with db.connection() as conn:
            db.q(conn, "SELECT 1").scalar_one()
    except Exception:  # noqa: BLE001 - health reports, never raises
        status, database = "degraded", "unavailable"
    return JSONResponse(
        {"status": status, "database": database, "time": timeutil.iso(timeutil.now())},
        status_code=200 if status == "ok" else 503,
    )
