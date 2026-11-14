"""Database engine and transaction helper (SQLAlchemy 2 Core, PyMySQL, synchronous).

All state lives in MariaDB (NFR-06). One request's writes happen in one transaction
(document 02, section 9): use `with transaction() as conn:`.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pymysql
from sqlalchemy import Connection, Engine, create_engine, event, text

from .config import get_settings

log = logging.getLogger("handshake.db")
_engine: Engine | None = None


_RETRY_CONNECT_CODES = {2003, 2013}  # can't connect / lost connection during the handshake


def make_engine(url: str) -> Engine:
    eng = create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=280,
        pool_size=5,
        max_overflow=5,
        hide_parameters=True,  # tracebacks must not print SQL parameters (doc 10, 3.1)
        connect_args={"init_command": "SET time_zone = '+00:00'", "charset": "utf8mb4",
                      "connect_timeout": 5},
    )

    @event.listens_for(eng, "do_connect")
    def _connect_with_retry(dialect, conn_rec, cargs, cparams):  # noqa: ANN001, ANN202
        """New connections occasionally stall or drop on a local port forwarder (seen with
        Docker Desktop's wslrelay). Fail fast and retry twice; log each retry."""
        for attempt in range(3):
            try:
                return dialect.connect(*cargs, **cparams)
            except pymysql.err.OperationalError as e:
                if e.args[0] not in _RETRY_CONNECT_CODES or attempt == 2:
                    raise
                log.warning("database connect failed with %s; retry %d", e.args[0], attempt + 1)
                time.sleep(0.3)
        return None  # unreachable

    return eng


def engine() -> Engine:
    global _engine
    if _engine is None:
        url = get_settings().database_url
        if not url:
            raise RuntimeError("DATABASE_URL is not set")
        _engine = make_engine(url)
    return _engine


def set_engine(e: Engine | None) -> None:
    """Tests: point the module at another database."""
    global _engine
    _engine = e


@contextmanager
def transaction() -> Iterator[Connection]:
    with engine().begin() as conn:
        yield conn


@contextmanager
def connection() -> Iterator[Connection]:
    with engine().connect() as conn:
        yield conn


def q(conn: Connection, sql: str, **params: Any):  # noqa: ANN201
    """Execute a textual statement with named parameters."""
    return conn.execute(text(sql), params)


def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def jload(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)
