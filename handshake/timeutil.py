"""Time helpers.

The database stores naive UTC DATETIME(3) (document 04). The API shows ISO 8601 with offset in
Europe/Helsinki (README conventions). `now()` is the single clock so tests can move time.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HELSINKI = ZoneInfo("Europe/Helsinki")

_offset = timedelta(0)


def now() -> datetime:
    """Current time, timezone-aware UTC, millisecond precision."""
    t = datetime.now(timezone.utc) + _offset
    return t.replace(microsecond=(t.microsecond // 1000) * 1000)


def shift_clock(delta: timedelta) -> None:
    """Tests only: move the clock forward."""
    global _offset
    _offset += delta


def reset_clock() -> None:
    global _offset
    _offset = timedelta(0)


def today_helsinki() -> date:
    return now().astimezone(HELSINKI).date()


def to_db(dt: datetime) -> datetime:
    """Aware datetime to naive UTC for storage."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime passed to to_db")
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def from_db(dt: datetime | None) -> datetime | None:
    """Naive UTC from the database to aware UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    """API representation: Europe/Helsinki with offset, second precision."""
    return dt.astimezone(HELSINKI).isoformat(timespec="seconds")


def iso_utc_ms(dt: datetime) -> str:
    """Hash-chain representation: UTC ISO 8601 with milliseconds (document 05, section 7.3)."""
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_upstream(value: str | None) -> datetime | None:
    """Tolerant ISO 8601 parse; values without an offset are Europe/Helsinki (document 06, 1.4)."""
    if value is None or value == "":
        return None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            d = date.fromisoformat(s[:10])
        except ValueError:
            return None
        dt = datetime(d.year, d.month, d.day)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=HELSINKI)
    return dt


def parse_upstream_date(value: str | None) -> date | None:
    """Date part of an upstream date or datetime string, read in Europe/Helsinki."""
    dt = parse_upstream(value)
    if dt is None:
        return None
    return dt.astimezone(HELSINKI).date()
