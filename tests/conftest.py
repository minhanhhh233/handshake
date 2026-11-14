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
            if t in CATALOG_TABLES:
                continue  # keep the copied catalog between runs; it is slow to rebuild
            conn.exec_driver_sql(f"DROP TABLE `{t}`")
        conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
        for stmt in _statements(SCHEMA.read_text(encoding="utf-8")):
            m = re.match(r"CREATE TABLE (\w+)", stmt)
            if m and m.group(1) in tables and m.group(1) in CATALOG_TABLES:
                continue
            conn.exec_driver_sql(stmt)
    copy_catalog(eng)
    db.set_engine(eng)
    yield eng
    db.set_engine(None)
    eng.dispose()


TRADING_TABLES = [
    "idempotency_keys", "approvals", "orders", "stock_holds", "offer_lines", "offers",
    "events", "price_quotes", "availability_snapshots",
]
PARTY_TABLES = ["mandates", "agents", "customers", "sandboxes", "sandbox_overlays", "proposals"]
CATALOG_TABLES = ["product_claims", "product_allergens", "product_labels",
                  "product_substitutions", "products", "code_lookup"]


def wipe(eng, tables: list[str]) -> None:
    with eng.begin() as conn:
        conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        for t in tables:
            conn.exec_driver_sql(f"DELETE FROM `{t}`")
        conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")


def _source_schema() -> str | None:
    """Database named in DATABASE_URL (the local dev DB with the loaded challenge data)."""
    url = os.environ.get("DATABASE_URL", "")
    name = url.rsplit("/", 1)[-1].split("?")[0] if url else ""
    return name or None


def copy_catalog(eng) -> bool:
    """Copy the catalog from the dev database on the same server. Data never leaves the
    machine and is never committed. Returns False when no loaded catalog is available."""
    src = _source_schema()
    target = eng.url.database
    if not src or src == target:
        return False
    try:
        with eng.begin() as conn:
            n = conn.exec_driver_sql(f"SELECT COUNT(*) FROM `{src}`.products").scalar_one()
            if not n:
                return False
            have = conn.exec_driver_sql("SELECT COUNT(*) FROM products").scalar_one()
            subs = conn.exec_driver_sql("SELECT COUNT(*) FROM product_substitutions").scalar_one()
            src_subs = conn.exec_driver_sql(
                f"SELECT COUNT(*) FROM `{src}`.product_substitutions").scalar_one()
            if have == n and subs == src_subs:
                return True  # already copied
            conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
            for t in CATALOG_TABLES:
                conn.exec_driver_sql(f"DELETE FROM `{t}`")
                conn.exec_driver_sql(f"INSERT INTO `{t}` SELECT * FROM `{src}`.`{t}`")
            conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
        return True
    except Exception:  # noqa: BLE001 - no dev catalog available
        return False


@pytest.fixture(scope="session")
def has_catalog(test_engine) -> bool:
    with test_engine.connect() as conn:
        return bool(conn.exec_driver_sql("SELECT COUNT(*) FROM products").scalar_one())


@pytest.fixture
def catalog(test_engine, has_catalog):
    """Skip unless the real challenge catalog was copied into the test database."""
    if not has_catalog:
        pytest.skip("challenge catalog not loaded in the dev database (scripts/load_data.py)")
    return test_engine


@pytest.fixture
def clean_db(test_engine):
    """Fresh trading and party tables for each test. The catalog is kept."""
    wipe(test_engine, TRADING_TABLES + PARTY_TABLES)
    timeutil.reset_clock()
    yield test_engine
    timeutil.reset_clock()
