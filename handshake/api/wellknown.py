"""Discovery, key set and health (document 03, section 2)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import db, timeutil

router = APIRouter(tags=["discovery"])


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
