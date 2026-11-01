"""Comparaison réponse générée / réponse de référence (benchmark/answer_compare.py)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "benchmark"))
from answer_compare import compare  # noqa: E402

REF_COLS, REF = ["sales_team", "avg"], [["Sales", 1914.625], ["Pre-Sales", 669.2857142857143]]


def test_same_values_in_another_order_and_with_extra_columns():
    gen_cols = ["team", "orders", "average_order_value"]
    gen = [["Pre-Sales", 7, 669.29], ["Sales", 8, 1914.62]]
    out = compare(REF_COLS, REF, gen_cols, gen)
    assert out["match"] and out["mapping"] == {"sales_team": "team", "avg": "average_order_value"}


def test_wrong_value_count_or_order():
    assert not compare(REF_COLS, REF, REF_COLS, REF[:1])["match"]
    assert "valeurs" in compare(REF_COLS, REF, REF_COLS, [["Sales", 1914.6], ["Pre-Sales", 669.29]])["reason"]
    assert compare(REF_COLS, REF, REF_COLS, REF[::-1])["match"]
    assert not compare(REF_COLS, REF, REF_COLS, REF[::-1], ordered=True)["match"]


def test_pairs_must_stay_together():
    ref = [["A", 1], ["B", 2]]
    assert not compare(["n", "v"], ref, ["n", "v"], [["A", 2], ["B", 1]])["match"]


def test_empty_results_and_stock_case():
    assert compare(["journal", "total"], [], ["j", "t"], [])["match"]
    assert not compare(["category", "value"], [["Office Furniture", 526515.5]],
                       ["category", "value"], [["Office Furniture", 0]])["match"]
