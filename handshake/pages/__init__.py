"""Human pages (phase 3): approval page and operator console. Server-rendered, outside /v1."""

from __future__ import annotations

from fastapi import APIRouter

from . import approve, console

router = APIRouter(include_in_schema=False)
router.include_router(approve.router)
router.include_router(console.router)

__all__ = ["router"]
