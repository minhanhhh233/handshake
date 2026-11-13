"""Shared pieces for the human pages: Jinja2 environment, formatting filters, headers."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import timeutil

TEMPLATES = Path(__file__).resolve().parent / "templates"

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


def _as_date(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.astimezone(timeutil.HELSINKI).date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _as_dt(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    return timeutil.parse_upstream(str(v))


def long_date(v: Any) -> str:
    """'Monday 16 November 2026'."""
    d = _as_date(v)
    if d is None:
        return ""
    return f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]} {d.year}"


def hm(v: Any) -> str:
    """'10:15' in Europe/Helsinki."""
    dt = _as_dt(v)
    return dt.astimezone(timeutil.HELSINKI).strftime("%H:%M") if dt else ""


def hms(v: Any) -> str:
    dt = _as_dt(v)
    return dt.astimezone(timeutil.HELSINKI).strftime("%H:%M:%S") if dt else ""


def when(v: Any) -> str:
    """'10:15 today', or '10:15 on 16 November' for another day (Europe/Helsinki)."""
    dt = _as_dt(v)
    if dt is None:
        return ""
    local = dt.astimezone(timeutil.HELSINKI)
    if local.date() == timeutil.today_helsinki():
        return f"{local:%H:%M} today"
    return f"{local:%H:%M} on {local.day} {MONTHS[local.month - 1]}"


def qty(v: Any) -> str:
    """Quantity with thousands separators and no trailing zeros: 1000 -> '1,000'."""
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return str(v)
    d = d.quantize(Decimal(1)) if d == d.to_integral_value() else d.normalize()
    return f"{d:,}"


def money(v: Any) -> str:
    """Money string as stored (two decimals); grouping is left out so the demo copy reads
    '1010.00 EUR' as in the documents."""
    if v is None:
        return ""
    try:
        return f"{Decimal(str(v)):.2f}"
    except (InvalidOperation, ValueError):
        return str(v)


env = Environment(
    loader=FileSystemLoader(str(TEMPLATES)),
    autoescape=select_autoescape(["html"], default_for_string=True, default=True),
    trim_blocks=True,
    lstrip_blocks=True,
)
env.filters.update(long_date=long_date, hm=hm, hms=hms, when=when, qty=qty, money=money)


# The approval token is in the URL: never let it reach a third party through Referer, a
# cache or a framing page (document 08, section 0, phase 3 must-do).
FONT_CSS = "https://fonts.googleapis.com"
FONT_FILES = "https://fonts.gstatic.com"

APPROVAL_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        f"default-src 'none'; style-src 'unsafe-inline' {FONT_CSS}; font-src {FONT_FILES}; "
        "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
}

CONSOLE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        f"default-src 'none'; style-src 'unsafe-inline' {FONT_CSS}; font-src {FONT_FILES}; "
        "script-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


def render(name: str, *, status: int = 200, headers: dict[str, str] | None = None,
           **ctx: Any) -> HTMLResponse:
    html = env.get_template(name).render(**ctx)
    return HTMLResponse(html, status_code=status, headers=dict(headers or {}))
