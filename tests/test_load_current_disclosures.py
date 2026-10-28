
import datetime
from unittest.mock import MagicMock

import pytest

import src.load_current_disclosures as loader


def record(claim_id, source_id, value, year=2025,
           metric="equity_interest_pct", normalized=None):
    return {
        "claim_id": claim_id,
        "report_year": year,
        "period_end": datetime.date(year, 9, 30),
        "source_id": source_id,
        "entity": "Example Corporation",
        "metric": metric,
        "value": value,
        "normalized_value": (
            normalized if normalized is not None else value
        ),
        "source_location": "Page 12",
    }


def make_connection(records):
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.fetchall.return_value = records
    return connection, cursor


COMPETING = [
    record(41, 7, "49"),
    record(58, 8, "51"),
]


def test_strict_rejects_competing_claims():
    connection, _ = make_connection(COMPETING)

    with pytest.raises(ValueError, match="Multiple current claims"):
        loader.load_current_disclosures(
            case_id=3, connection=connection
        )


def test_non_strict_returns_all_competing_claims():
    connection, _ = make_connection(COMPETING)

    df = loader.load_current_disclosures(
        case_id=3, strict=False, connection=connection
    )

    assert df["claim_id"].tolist() == ["41", "58"]
    assert df["source_id"].tolist() == ["7", "8"]
    assert df["value"].tolist() == ["49", "51"]
    assert df["normalized_value"].tolist() == ["49", "51"]
    assert df["period_end"].tolist() == ["2025-09-30"] * 2
    assert df["report_year"].tolist() == ["2025"] * 2


def test_query_parameters_follow_years():
    connection, cursor = make_connection([record(1, 2, "49")])

    loader.load_current_disclosures(
        case_id=9, years=(2022, 2023, 2024), connection=connection
    )

    sql, params = cursor.execute.call_args.args
    assert "IN (%s, %s, %s)" in sql
    assert "v_current_disclosure_claims" in sql
    assert params == (9, 2022, 2023, 2024)


@pytest.mark.parametrize("years", [(), ("2024",), (2024.0,), (True,)])
def test_invalid_years_rejected(years):
    connection, _ = make_connection([])

    with pytest.raises(ValueError, match="years"):
        loader.load_current_disclosures(
            years=years, connection=connection
        )


def test_stored_normalization_mismatch_raises():
    connection, _ = make_connection([
        record(12, 2, "49.0%", normalized="49.0%"),
    ])

    with pytest.raises(ValueError, match=r"\['12'\]"):
        loader.load_current_disclosures(connection=connection)


def test_caller_owned_connection_untouched():
    connection, cursor = make_connection([record(1, 2, "49")])

    loader.load_current_disclosures(connection=connection)

    connection.close.assert_not_called()
    connection.commit.assert_not_called()
    connection.rollback.assert_not_called()
    connection.start_transaction.assert_not_called()
    cursor.close.assert_called_once()


def test_owned_connection_is_read_only_and_closed(monkeypatch):
    connection, _ = make_connection([record(1, 2, "49")])
    monkeypatch.setattr(loader, "_connect", lambda: connection)

    loader.load_current_disclosures()

    connection.start_transaction.assert_called_once_with(
        readonly=True
    )
    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()
    connection.close.assert_called_once()


def test_owned_connection_closed_on_error(monkeypatch):
    connection, cursor = make_connection([])
    cursor.execute.side_effect = RuntimeError("query failed")
    monkeypatch.setattr(loader, "_connect", lambda: connection)

    with pytest.raises(RuntimeError, match="query failed"):
        loader.load_current_disclosures()

    connection.rollback.assert_called_once()
    connection.close.assert_called_once()


def test_no_records_raises():
    connection, _ = make_connection([])

    with pytest.raises(ValueError, match="No current"):
        loader.load_current_disclosures(connection=connection)
