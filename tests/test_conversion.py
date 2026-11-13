"""Quantity conversion, document 09 section 2.2 (C-01 to C-07). Pure functions, no database."""

from __future__ import annotations

import logging
from decimal import Decimal as D

from handshake.offers import convert, price_str, round_down_to_step, sales_units_needed


def test_c01_price_ratio_ten_litres():
    c = convert("L", D("10.00"), D("1.00"), "L", None, None)
    assert c.method == "price_ratio" and c.base_per_sales_unit == D("10")
    assert sales_units_needed(D(1000), c.base_per_sales_unit) == 100


def test_c02_price_ratio_one_litre():
    c = convert("L", D("1.05"), D("1.05"), "L", D("1.0"), None)
    assert c.method == "price_ratio" and c.base_per_sales_unit == 1
    assert sales_units_needed(D(1000), c.base_per_sales_unit) == 1000


def test_c03_twelve_litres_rounds_up():
    units = sales_units_needed(D(1000), D(12))
    assert units == 84
    assert units * 12 == 1008


def test_c04_product_data_when_comparison_unit_differs():
    c = convert("L", D("1.05"), D("1.02"), "KG", D("1.0"), D("1.03"))
    assert c.method == "product_data" and c.base_per_sales_unit == 1
    assert sales_units_needed(D(1000), c.base_per_sales_unit) == 1000


def test_c05_no_conversion():
    assert convert("L", D("1.05"), D("1.02"), "KG", None, D("1.03")) is None


def test_c06_lot_size_five():
    # raw need 83 units (83 L at 1 L per unit), allowed lot size 5 -> 85
    assert sales_units_needed(D(83), D(1), D(5)) == 85


def test_c07_zero_or_null_comparison_falls_through():
    for comp in (D(0), None):
        c = convert("L", D("1.05"), comp, "L", D("1.0"), None)
        assert c.method == "product_data" and c.base_per_sales_unit == 1


def test_ratio_rounded_to_four_decimals_and_kg():
    c = convert("KG", D("31.31"), D("1.57"), "kg", None, None)
    assert c.method == "price_ratio" and c.base_per_sales_unit == D("19.9427")


def test_disagreement_over_two_percent_uses_ratio_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="handshake.offers"):
        c = convert("L", D("10.00"), D("1.00"), "L", D("12"), None, product_id="x")
    assert c.method == "price_ratio" and c.base_per_sales_unit == 10
    assert any("disagree" in r.getMessage() for r in caplog.records)


def test_small_lot_whole_units_unless_variable():
    assert sales_units_needed(D("10.5"), D(1), D("0.01"), False) == 11
    assert sales_units_needed(D("10.5"), D(1), D("0.01"), True) == D("10.5")
    assert round_down_to_step(D("384.7"), D("0.01"), False) == 384
    assert round_down_to_step(D(52), D(5), False) == 50


def test_price_str():
    assert price_str(D("1.01")) == "1.01"
    assert price_str(D("1.0")) == "1.00"
    assert price_str(D("1.2345")) == "1.2345"
    assert price_str(D("1.23400")) == "1.234"
