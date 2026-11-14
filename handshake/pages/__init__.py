"""Human pages: approval, console, receipts and the public landing page with its guided demo.
Server-rendered, outside /v1."""

from __future__ import annotations

from fastapi import APIRouter

from . import approve, console, public, receipt

router = APIRouter(include_in_schema=False)
router.include_router(approve.router)
router.include_router(console.router)
router.include_router(receipt.router)
router.include_router(public.router)

__all__ = ["router"]
