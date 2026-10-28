
import pandas as pd
import pytest

from src.disclosure_conflicts import audit_disclosure_claims


def claim(
    source_id="1",
    value="49",
    period_end="2025-09-30",
    metric="shareholding_pct",
):
    return {
        "report_year": "2025",
        "period_end": period_end,
        "source_id": source_id,
        "entity": "Example Corporation",
        "metric": metric,
        "value": value,
        "source_location": "Page 12",
    }


def analyze(*records):
    return audit_disclosure_claims(
        pd.DataFrame(records)
    )


def test_cross_source_conflict():
    result = analyze(
        claim(source_id="1", value="49"),
        claim(source_id="2", value="51"),
    )

    assert result.iloc[0]["conflict_status"] == (
        "CROSS_SOURCE_CONFLICT"
    )


def test_equivalent_percentages():
    result = analyze(
        claim(source_id="1", value="49"),
        claim(source_id="2", value="49.0%"),
    )

    assert result.iloc[0]["conflict_status"] == (
        "INDEPENDENCE_UNVERIFIED"
    )


def test_different_reporting_periods():
    result = analyze(
        claim(source_id="1", value="49",
              period_end="2024-09-30"),
        claim(source_id="2", value="51",
              period_end="2025-09-30"),
    )

    assert len(result) == 2
    assert set(result["conflict_status"]) == {
        "SINGLE_CLAIM"
    }


def test_intra_source_conflict():
    result = analyze(
        claim(source_id="1", value="49"),
        claim(source_id="1", value="51"),
    )

    assert result.iloc[0]["conflict_status"] == (
        "INTRA_SOURCE_CONFLICT"
    )


def test_repeated_matching_claims():
    result = analyze(
        claim(source_id="1", value="49"),
        claim(source_id="1", value="49"),
    )

    assert result.iloc[0]["conflict_status"] == (
        "REPEATED_SOURCE_CLAIMS"
    )


def test_missing_source_id():
    with pytest.raises(ValueError):
        analyze(claim(source_id=""))


def test_invalid_percentage():
    with pytest.raises(ValueError):
        analyze(claim(value="150"))


def test_textual_conflict():
    result = analyze(
        claim(source_id="1", value="Company A",
              metric="controller"),
        claim(source_id="2", value="Company B",
              metric="controller"),
    )

    assert result.iloc[0]["conflict_status"] == (
        "CROSS_SOURCE_CONFLICT"
    )
