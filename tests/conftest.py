"""Shared test fixtures.

Database tests use TEST_DATABASE_URL (a throwaway database the suite may wipe). Without it,
tests marked `db` are skipped. Upstream calls are always replaced by fakes (document 09).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from handshake import config, db, timeutil

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "sql" / "schema.sql"

config._load_dotenv()


def _statements(sql: str) -> list[str]:
    sql = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in sql.split(";") if s.strip()]


@pytest.fixture(scope="session")
def test_engine():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    if "test" not in url.rsplit("/", 1)[-1]:
        pytest.exit("TEST_DATABASE_URL must name a database containing 'test'; it gets wiped")
    eng = db.make_engine(url)
    with eng.begin() as conn:
        conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        tables = [r[0] for r in conn.exec_driver_sql("SHOW TABLES")]
        for t in tables:
            conn.exec_driver_sql(f"DROP TABLE `{t}`")
        conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
        for stmt in _statements(SCHEMA.read_text(encoding="utf-8")):
            conn.exec_driver_sql(stmt)
    db.set_engine(eng)
    yield eng
    db.set_engine(None)
    eng.dispose()


TRADING_TABLES = [
    "idempotency_keys", "approvals", "orders", "stock_holds", "offer_lines", "offers",
    "events", "price_quotes", "availability_snapshots",
]
PARTY_TABLES = ["mandates", "agents", "customers"]
CATALOG_TABLES = ["product_claims", "product_allergens", "product_labels",
                  "product_substitutions", "products", "code_lookup"]


def wipe(eng, tables: list[str]) -> None:
    with eng.begin() as conn:
        conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        for t in tables:
            conn.exec_driver_sql(f"DELETE FROM `{t}`")
        conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")


@pytest.fixture
def clean_db(test_engine):
    """Empty database (schema only) for each test."""
    wipe(test_engine, TRADING_TABLES + PARTY_TABLES + CATALOG_TABLES)
    timeutil.reset_clock()
    yield test_engine
    timeutil.reset_clock()
