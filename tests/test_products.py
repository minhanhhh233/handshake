"""Product search and detail API (tasks T-101, T-102; document 09, section 3)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from handshake import db
from handshake.api import create_app
from handshake.ids import new_id

from .factories import make_party

pytestmark = pytest.mark.db

LACTOSE_FREE_MILK = "/v1/products?category=10400&claims=FREE_FROM_LACTOSE&limit=50"
PLAIN_MILK_EXCLUDES = "sour,piimä,coffee,kahvi,latte,portion,annos,café,cafe"
FORBIDDEN_WORDS = ("price", "availab", "stock", "amount")


@pytest.fixture
def client(catalog, clean_db) -> TestClient:
    return TestClient(create_app())


def _keys(obj: Any) -> list[str]:
    if isinstance(obj, dict):
        return [k for k in obj] + [k2 for v in obj.values() for k2 in _keys(v)]
    if isinstance(obj, list):
        return [k for v in obj for k in _keys(v)]
    return []


def _event_count(type_: str) -> int:
    with db.connection() as conn:
        return db.q(conn, "SELECT COUNT(*) FROM events WHERE type = :t", t=type_).scalar_one()


def test_api01_lactose_free_milk_authenticated(client):
    p = make_party()
    r = client.get(LACTOSE_FREE_MILK, headers=p.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 35 and len(body["items"]) == 35
    assert body["next_cursor"] is None
    for item in body["items"]:
        assert "FREE_FROM_LACTOSE" in item["claims"]
        assert item["category"]["code"] == "10400"
        # All 35 carry AM CONTAINS: lactose-free is not milk-free (document 06, 4.1).
        assert {"code": "AM", "name": "Milk", "state": "CONTAINS"} in item["allergens"]


def test_api02_anonymous_search_has_no_price_or_availability(client):
    r = client.get(LACTOSE_FREE_MILK)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 35
    bad = [k for k in _keys(body) if any(w in k.lower() for w in FORBIDDEN_WORDS)]
    assert bad == []
    detail = client.get("/v1/products/464306").json()
    assert [k for k in _keys(detail) if any(w in k.lower() for w in FORBIDDEN_WORDS)] == []


def _assert_all_free_from(items: list[dict], code: str) -> None:
    for item in items:
        states = {a["code"]: a["state"] for a in item["allergens"]}
        assert states.get(code) == "FREE_FROM"
        assert item["allergen_data"] == "present"


def test_api03_exclude_allergens_requires_free_from(client):
    r = client.get("/v1/products?exclude_allergens=AM&category=10400&limit=50")
    assert r.status_code == 200, r.text
    # No product in 10400 states AM FREE_FROM; every milk product contains milk or says
    # nothing, so none may pass (missing data is not safe).
    _assert_all_free_from(r.json()["items"], "AM")
    # Across the catalog there are milk-free products, and only FREE_FROM ones pass.
    r = client.get("/v1/products?exclude_allergens=AM,AE&limit=50")
    items = r.json()["items"]
    assert len(items) == 50
    _assert_all_free_from(items, "AM")
    _assert_all_free_from(items, "AE")


def test_api04_detail_substitutes_and_untrusted_text(client):
    r = client.get("/v1/products/464306")
    assert r.status_code == 200, r.text
    d = r.json()
    assert sorted(d["substitutes"]) == sorted(["465039", "464398", "469363", "464201"])
    assert d["has_substitution_rules"] is True
    assert d["untrusted_text"]["notice"].startswith("Supplier-provided free text")
    assert "en" in d["untrusted_text"]["marketing"]
    assert d["gtin"] == "6408430000487"
    assert d["case_size_in_base_units"] == 20
    assert d["sales_unit"] == {"code": "ST", "name": "Piece"}
    assert d["category"] == {"code": "10400", "name": "Milk and milk drinks"}
    assert d["comparison"] == {"delivery_reliability": 1.0, "carbon_kg_co2e_per_kg": 1.1,
                               "carbon_basis": "product_group_average"}
    assert {"unit": "KI", "size_in_base_units": 20, "gtin": "6408430101009"} in d["units"]
    assert d["ingredient_origins"] == [{"ingredient": "milk", "type": "MILKING",
                                        "country": "246"}]
    ca = next(n for n in d["nutrition"] if n["id"] == "CA")
    assert ca == {"id": "CA", "value": 110, "unit": "MGM", "per": {"value": 100, "unit": "GRM"}}
    assert d["allowed_lot_size"] == 0.01 and d["temperature_condition"] == 4


def test_name_excludes_leaves_25_plain_milks(client):
    # Accent-insensitive: "cafe" also drops 463380 "Arla Cafè milk" (doc 06, section 4.1).
    r = client.get(f"{LACTOSE_FREE_MILK}&name_excludes={PLAIN_MILK_EXCLUDES}")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 25
    assert "463380" not in {i["product_id"] for i in items}
    for item in items:
        names = f"{item['name']['en']} {item['name']['fi']}".lower()
        assert not any(t in names for t in PLAIN_MILK_EXCLUDES.split(",") if t != "cafe")


def test_paging_walks_all_35_without_duplicates(client):
    seen: list[str] = []
    url = "/v1/products?category=10400&claims=FREE_FROM_LACTOSE&limit=10"
    cursor = None
    for _ in range(10):
        r = client.get(url + (f"&cursor={cursor}" if cursor else ""))
        assert r.status_code == 200, r.text
        body = r.json()
        seen += [i["product_id"] for i in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 35 and len(set(seen)) == 35
    full = client.get(LACTOSE_FREE_MILK).json()["items"]
    assert seen == [i["product_id"] for i in full]
    # Reliability ordering: non-null values first, descending.
    rel = [i["comparison"].get("delivery_reliability") for i in full]
    known = [x for x in rel if x is not None]
    assert rel[: len(known)] == sorted(known, reverse=True)


def test_fulltext_query_and_offset_paging(client):
    r = client.get("/v1/products?q=lactose&category=10400&limit=5")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 5 and body["next_cursor"]
    r2 = client.get(f"/v1/products?q=lactose&category=10400&limit=5&cursor={body['next_cursor']}")
    page2 = [i["product_id"] for i in r2.json()["items"]]
    assert not set(page2) & {i["product_id"] for i in body["items"]}
    # Short q falls back to LIKE.
    short = client.get("/v1/products?q=l&category=10400&limit=3")
    assert short.status_code == 200 and short.json()["count"] == 3


def test_bad_cursor_and_limit_are_invalid_request(client):
    r = client.get("/v1/products?cursor=not-a-cursor")
    assert r.status_code == 400 and r.json()["code"] == "invalid_request"
    r = client.get("/v1/products?limit=51")
    assert r.status_code == 400 and r.json()["code"] == "invalid_request"


def test_unknown_product_is_404_problem(client):
    r = client.get("/v1/products/does-not-exist")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["code"] == "not_found" and body["status"] == 404
    assert body["trace_id"] == r.headers["X-Trace-Id"]


def test_events_only_for_authenticated_with_trace(client):
    p = make_party()
    trace = new_id("trc")
    # Anonymous with a trace: no event.
    client.get(LACTOSE_FREE_MILK, headers={"X-Trace-Id": trace})
    client.get("/v1/products/464306", headers={"X-Trace-Id": trace})
    # Authenticated without a trace: no event.
    client.get(LACTOSE_FREE_MILK, headers=p.headers)
    client.get("/v1/products/464306", headers=p.headers)
    assert _event_count("search.performed") == 0 and _event_count("product.read") == 0

    h = {**p.headers, "X-Trace-Id": trace}
    r = client.get(LACTOSE_FREE_MILK, headers=h)
    assert r.headers["X-Trace-Id"] == trace
    client.get("/v1/products/464306", headers=h)
    with db.connection() as conn:
        rows = db.q(conn, "SELECT type, actor_type, actor_id, audience, body_json, text "
                          "FROM events WHERE trace_id = :t ORDER BY seq", t=trace).mappings().all()
    assert [r["type"] for r in rows] == ["search.performed", "product.read"]
    for row in rows:
        assert row["actor_type"] == "agent" and row["actor_id"] == p.agent_id
        assert row["audience"] == "aimo_only" and row["text"]
    search_body = db.jload(rows[0]["body_json"])
    assert search_body["result_count"] == 35
    assert search_body["filters"]["claims"] == ["FREE_FROM_LACTOSE"]
    assert db.jload(rows[1]["body_json"])["product_id"] == "464306"
