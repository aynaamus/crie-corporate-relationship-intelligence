
import pandas as pd
import pytest

from src.comparison_rules import classify


def make_record(metric, old, new, merge="both"):
    return pd.Series({
        "metric": metric,
        "value_2024": old,
        "value_2025": new,
        "_merge": merge,
    })


def test_unchanged_percentage():
    row = make_record("equity_interest_pct", "49", "49.0")
    result = classify(row)

    assert result["comparison_status"] == "UNCHANGED"
    assert result["change_percentage_points"] == "0"


def test_increased_percentage():
    row = make_record("equity_interest_pct", "49", "51")
    result = classify(row)

    assert result["comparison_status"] == "CHANGED"
    assert result["change_percentage_points"] == "2"


def test_decreased_percentage():
    row = make_record("equity_interest_pct", "51", "49")
    result = classify(row)

    assert result["comparison_status"] == "CHANGED"
    assert result["change_percentage_points"] == "-2"


def test_percentage_with_percent_symbol():
    row = make_record("equity_interest_pct", "49%", "49.0%")
    result = classify(row)

    assert result["comparison_status"] == "UNCHANGED"


def test_unchanged_company_name():
    row = make_record(
        "management_controller",
        "Slovak Gas Holding B.V.",
        "slovak gas holding b.v.",
    )
    result = classify(row)

    assert result["comparison_status"] == "UNCHANGED"


def test_changed_company_name():
    row = make_record(
        "management_controller",
        "Company A",
        "Company B",
    )
    result = classify(row)

    assert result["comparison_status"] == "CHANGED"


def test_missing_reporting_period():
    row = make_record(
        "equity_interest_pct",
        "49",
        "",
        merge="left_only",
    )
    result = classify(row)

    assert result["comparison_status"] == "MISSING_PERIOD"
    assert result["change_percentage_points"] == ""


@pytest.mark.parametrize(
    "invalid_value",
    ["unknown", "-5", "125", "NaN", "Infinity"],
)
def test_invalid_percentages(invalid_value):
    row = make_record(
        "equity_interest_pct",
        "49",
        invalid_value,
    )

    with pytest.raises(ValueError):
        classify(row)


def test_text_whitespace_and_case_unchanged():
    row = make_record(
        "management_controller",
        "Slovak Gas  Holding B.V.",
        " SLOVAK GAS HOLDING B.V. ",
    )
    result = classify(row)

    assert result["comparison_status"] == "UNCHANGED"


def test_different_legal_names_remain_changed():
    # Lexical normalization must not merge entities.
    row = make_record(
        "management_controller",
        "Slovak Gas Holding B.V.",
        "Slovak Gas Holding BV",
    )
    result = classify(row)

    assert result["comparison_status"] == "CHANGED"


def test_custom_value_columns():
    row = pd.Series({
        "metric": "equity_interest_pct",
        "value_2023": "49",
        "value_2024": "51",
        "_merge": "both",
    })
    result = classify(
        row,
        previous="value_2023",
        current="value_2024",
    )

    assert result["comparison_status"] == "CHANGED"
    assert result["change_percentage_points"] == "2"
