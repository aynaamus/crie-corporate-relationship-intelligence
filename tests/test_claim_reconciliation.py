
import importlib
import json
import sys

import mysql.connector
import pandas as pd
import pytest

from src.claim_reconciliation import (
    build_reconciled_comparison,
    check_period_compatibility,
    reconcile_claims,
)
from src.disclosure_conflicts import audit_disclosure_claims


_next_id = iter(range(500, 10_000, 7))


def claim(source_id, value, year=2025, period_end=None,
          metric="equity_interest_pct",
          entity="Northwind Holdings plc",
          location="Page 12", claim_id=None):
    return {
        "claim_id": str(claim_id or next(_next_id)),
        "report_year": str(year),
        "period_end": period_end or f"{year}-09-30",
        "source_id": str(source_id),
        "entity": entity,
        "metric": metric,
        "value": value,
        "source_location": location,
    }


def reconcile(*records):
    return reconcile_claims(pd.DataFrame(records))


def compare(*records, **years):
    return build_reconciled_comparison(
        pd.DataFrame(records), **years
    )


# --- Same-period reconciliation -------------------------------


def test_single_claim_is_not_corroborated():
    row = reconcile(claim(1, "49")).iloc[0]

    assert row["reconciliation_status"] == "SINGLE_CLAIM"
    assert not row["corroborated"]
    assert row["source_count"] == 1
    assert row["reconciled_value"] == "49"


def test_two_sources_agreeing_are_not_corroboration_by_themselves():
    # Agreement of two registered sources says nothing about
    # whether they are independent (A1).
    row = reconcile(
        claim(1, "49", claim_id=11),
        claim(2, "49", claim_id=12),
    ).iloc[0]

    assert row["reconciliation_status"] == "INDEPENDENCE_UNVERIFIED"
    assert not row["corroborated"]
    assert row["claim_ids"] == "11;12"
    assert row["source_ids"] == "1;2"


def test_equivalent_numeric_representations():
    row = reconcile(
        claim(1, "49"),
        claim(2, "49.0"),
        claim(3, "49%"),
    ).iloc[0]

    assert row["reconciliation_status"] == "INDEPENDENCE_UNVERIFIED"
    assert row["distinct_value_count"] == 1
    # Originals differ in form, so the shared normalized
    # value is reported rather than one source's text.
    assert row["reconciled_value"] == "49"


def test_two_agree_one_disagrees_is_unresolved():
    row = reconcile(
        claim(1, "49", claim_id=21, location="Page 3"),
        claim(2, "49%", claim_id=22, location="Note 7"),
        claim(3, "51", claim_id=23, location="Page 40"),
    ).iloc[0]

    assert row["reconciliation_status"] == "UNRESOLVED_CONFLICT"
    assert row["conflict_scope"] == "CROSS_SOURCE"
    # No majority vote: nothing is selected.
    assert row["reconciled_value"] == ""

    details = json.loads(row["claim_details"])
    assert [d["claim_id"] for d in details] == ["21", "22", "23"]
    assert [d["value"] for d in details] == ["49", "49%", "51"]
    assert [d["source_location"] for d in details] == [
        "Page 3", "Note 7", "Page 40",
    ]


def test_within_source_conflict():
    row = reconcile(
        claim(1, "49"),
        claim(1, "51"),
    ).iloc[0]

    assert row["reconciliation_status"] == "UNRESOLVED_CONFLICT"
    assert row["conflict_scope"] == "WITHIN_SOURCE"


def test_within_and_cross_source_conflict():
    row = reconcile(
        claim(1, "49"),
        claim(1, "51"),
        claim(2, "49"),
    ).iloc[0]

    assert row["conflict_scope"] == "CROSS_SOURCE;WITHIN_SOURCE"


def test_repeated_claims_from_one_source_not_corroboration():
    row = reconcile(
        claim(1, "49", location="Page 12"),
        claim(1, "49.0", location="Page 30"),
    ).iloc[0]

    assert row["reconciliation_status"] == "REPEATED_SOURCE_CLAIMS"
    assert not row["corroborated"]
    assert row["source_count"] == 1
    assert row["claim_count"] == 2


def test_similar_company_names_not_merged():
    result = reconcile(
        claim(1, "49", entity="Northwind Holdings plc"),
        claim(2, "51", entity="Northwind Holding plc"),
    )

    assert len(result) == 2
    assert set(result["reconciliation_status"]) == {"SINGLE_CLAIM"}


def test_different_period_ends_not_grouped():
    result = reconcile(
        claim(1, "49", period_end="2025-09-30"),
        claim(2, "51", period_end="2025-12-31"),
    )

    assert len(result) == 2
    assert set(result["reconciliation_status"]) == {"SINGLE_CLAIM"}


def test_textual_values_normalized_not_merged():
    result = reconcile(
        claim(1, "Example Parent B.V.", metric="controller"),
        claim(2, "example  parent b.v.", metric="controller"),
        claim(3, "Example Parent BV", metric="controller"),
    ).iloc[0]

    assert result["reconciliation_status"] == "UNRESOLVED_CONFLICT"
    assert result["distinct_value_count"] == 2


def test_input_not_modified():
    claims = pd.DataFrame([claim(1, " 49% ")])
    before = claims.copy()

    reconcile_claims(claims)

    pd.testing.assert_frame_equal(claims, before)


def test_duplicate_claim_ids_rejected():
    with pytest.raises(ValueError, match="Duplicate claim IDs"):
        reconcile(claim(1, "49", claim_id=5), claim(2, "49", claim_id=5))


def test_missing_claim_id_column_rejected():
    df = pd.DataFrame([claim(1, "49")]).drop(columns=["claim_id"])

    with pytest.raises(ValueError, match="claim_id"):
        reconcile_claims(df)


def test_conflict_detection_agrees_with_audit():
    records = [
        claim(1, "49"), claim(2, "49.0%"),
        claim(1, "51", metric="other_pct"),
        claim(2, "52", metric="other_pct"),
        claim(3, "10", metric="third_pct"),
    ]

    reconciled = reconcile(*records)
    audit = audit_disclosure_claims(pd.DataFrame(records))

    conflict = reconciled["reconciliation_status"] == "UNRESOLVED_CONFLICT"
    audited = audit["conflict_status"].str.endswith("_CONFLICT")

    assert conflict.tolist() == audited.tolist()


# --- Reporting-period compatibility --------------------------


@pytest.mark.parametrize(
    "previous,current,gap,expected",
    [
        ("2024-09-30", "2025-09-30", 1, ""),
        ("2024-12-31", "2025-12-31", 1, ""),
        ("2024-09-30", "2025-12-31", 1, "FISCAL_YEAR_END_DIFFERS"),
        ("2024-09-30", "2026-09-30", 1, "PERIOD_INTERVAL_MISMATCH"),
        ("2023-09-30", "2025-09-30", 2, ""),
        ("2024-02-29", "2025-02-28", 1, "FISCAL_YEAR_END_DIFFERS"),
        ("2024-13-01", "2025-09-30", 1, "INVALID_PERIOD_END"),
        ("unknown", "2025-09-30", 1, "INVALID_PERIOD_END"),
    ],
)
def test_period_compatibility(previous, current, gap, expected):
    assert check_period_compatibility(previous, current, gap) == expected


# --- Reconciled year-over-year comparison ---------------------


def test_compatible_periods_unchanged():
    row = compare(
        claim(1, "49", year=2024),
        claim(2, "49.0%", year=2025),
    ).iloc[0]

    assert row["comparison_status"] == "UNCHANGED"
    assert row["change_percentage_points"] == "0"
    assert row["comparison_flags"] == ""


def test_change_reported_by_agreeing_sources():
    row = compare(
        claim(1, "49", year=2024, claim_id=31),
        claim(3, "51", year=2025, claim_id=32),
        claim(4, "51%", year=2025, claim_id=33),
    ).iloc[0]

    assert row["comparison_status"] == "CHANGED"
    assert row["change_percentage_points"] == "2"
    assert row["reconciliation_2025"] == "INDEPENDENCE_UNVERIFIED"
    assert row["claim_ids_2025"] == "32;33"
    assert row["source_2025"] == "3;4"


def test_incompatible_fiscal_year_end_is_period_mismatch():
    row = compare(
        claim(1, "49", year=2024, period_end="2024-09-30"),
        claim(2, "49", year=2025, period_end="2025-12-31"),
    ).iloc[0]

    assert row["comparison_status"] == "PERIOD_MISMATCH"
    assert row["period_detail"] == "FISCAL_YEAR_END_DIFFERS"
    assert row["change_percentage_points"] == ""
    # Both values are preserved, not compared.
    assert row["value_2024"] == "49"
    assert row["value_2025"] == "49"


def test_multiple_periods_in_one_year_flagged():
    row = compare(
        claim(1, "49", year=2024, period_end="2024-09-30"),
        claim(2, "49", year=2025, period_end="2025-09-30"),
        claim(3, "50", year=2025, period_end="2025-12-31"),
    ).iloc[0]

    assert row["comparison_status"] == "PERIOD_MISMATCH"
    assert row["period_detail"] == "MULTIPLE_PERIODS_IN_YEAR"
    assert row["period_end_2025"] == "2025-09-30;2025-12-31"
    # No arbitrary period selected.
    assert row["value_2025"] == ""


def test_missing_period():
    row = compare(claim(1, "49", year=2024, claim_id=41)).iloc[0]

    assert row["comparison_status"] == "MISSING_PERIOD"
    assert row["comparison_flags"] == "MISSING_PERIOD"
    assert row["claim_ids_2024"] == "41"
    assert row["claim_ids_2025"] == ""


def test_conflict_propagates_into_comparison():
    row = compare(
        claim(1, "49", year=2024),
        claim(2, "49", year=2025, claim_id=51),
        claim(3, "51", year=2025, claim_id=52),
    ).iloc[0]

    assert row["comparison_status"] == "UNRESOLVED_CONFLICT"
    assert row["reconciliation_2025"] == "UNRESOLVED_CONFLICT"
    assert row["value_2025"] == ""
    assert row["change_percentage_points"] == ""

    evidence = json.loads(row["evidence_2025"])
    assert {e["claim_id"] for e in evidence} == {"51", "52"}
    assert {e["value"] for e in evidence} == {"49", "51"}


@pytest.mark.parametrize(
    "records,status,flags",
    [
        # Conflict outranks a period mismatch.
        (
            [
                claim(1, "49", year=2024, period_end="2024-09-30"),
                claim(2, "49", year=2025, period_end="2025-12-31"),
                claim(3, "51", year=2025, period_end="2025-12-31"),
            ],
            "UNRESOLVED_CONFLICT",
            "UNRESOLVED_CONFLICT;PERIOD_MISMATCH",
        ),
        # Conflict outranks a missing period.
        (
            [claim(1, "49", year=2024), claim(2, "51", year=2024)],
            "UNRESOLVED_CONFLICT",
            "UNRESOLVED_CONFLICT;MISSING_PERIOD",
        ),
        # Ambiguous periods outrank a missing period.
        (
            [
                claim(1, "49", year=2024, period_end="2024-06-30"),
                claim(2, "49", year=2024, period_end="2024-12-31"),
            ],
            "PERIOD_MISMATCH",
            "PERIOD_MISMATCH;MISSING_PERIOD",
        ),
    ],
)
def test_status_precedence(records, status, flags):
    row = compare(*records).iloc[0]

    assert row["comparison_status"] == status
    assert row["comparison_flags"] == flags


def test_configurable_years():
    row = compare(
        claim(1, "49", year=2021, period_end="2021-12-31"),
        claim(2, "52", year=2023, period_end="2023-12-31"),
        previous_year=2021,
        current_year=2023,
    ).iloc[0]

    assert row["comparison_status"] == "CHANGED"
    assert row["change_percentage_points"] == "3"


def test_unexpected_year_rejected():
    with pytest.raises(ValueError, match="Unexpected"):
        compare(claim(1, "49", year=2022))


def test_all_claim_and_source_ids_preserved():
    records = [
        claim(1, "49", year=2024, claim_id=61),
        claim(2, "49", year=2024, claim_id=62),
        claim(3, "49", year=2025, claim_id=63),
        claim(3, "49.0", year=2025, claim_id=64),
        claim(4, "51", year=2025, claim_id=65),
    ]

    row = compare(*records).iloc[0]

    assert row["claim_ids_2024"] == "61;62"
    assert row["claim_ids_2025"] == "63;64;65"
    assert row["source_2024"] == "1;2"
    assert row["source_2025"] == "3;4"


def test_pure_functions_do_not_touch_database(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Database connection attempted")

    monkeypatch.setattr(mysql.connector, "connect", fail)
    monkeypatch.setattr(pd.DataFrame, "to_csv", fail)
    monkeypatch.delitem(
        sys.modules, "src.claim_reconciliation", raising=False
    )

    module = importlib.import_module("src.claim_reconciliation")

    module.build_reconciled_comparison(pd.DataFrame([
        claim(1, "49", year=2024),
        claim(2, "49", year=2025),
    ]))
