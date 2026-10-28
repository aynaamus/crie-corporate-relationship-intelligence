
import pytest

from src.claim_corrections import prepare_replacement
from unittest.mock import MagicMock

from src.claim_corrections import correct_claim


@pytest.fixture
def original_claim():
    return {
        "claim_id": 100,
        "source_id": 200,
        "entity_name": "Example Corporation",
        "metric": "equity_interest_pct",
        "report_year": 2025,
        "period_end": "2025-12-31",
        "value_text": "49",
        "source_location": "Page 12",
    }


def test_valid_correction(original_claim):
    result = prepare_replacement(
        original_claim,
        "48",
        "Page 12",
        "Transcription error",
    )

    assert result["value_text"] == "48"
    assert result["normalized_value"] == "48"
    assert len(result["claim_fingerprint"]) == 64


def test_original_remains_unchanged(original_claim):
    prepare_replacement(
        original_claim,
        "48",
        "Page 12",
        "Transcription error",
    )

    assert original_claim["value_text"] == "49"


def test_reject_identical_claim(original_claim):
    with pytest.raises(ValueError):
        prepare_replacement(
            original_claim,
            "49",
            "Page 12",
            "No actual change",
        )


def test_reject_invalid_percentage(original_claim):
    with pytest.raises(ValueError):
        prepare_replacement(
            original_claim,
            "125",
            "Page 12",
            "Invalid value",
        )


def test_require_correction_reason(original_claim):
    with pytest.raises(ValueError):
        prepare_replacement(
            original_claim,
            "48",
            "Page 12",
            "",
        )


def test_allow_location_correction(original_claim):
    result = prepare_replacement(
        original_claim,
        "49",
        "Page 14",
        "Incorrect page reference",
    )

    assert result["value_text"] == "49"
    assert result["source_location"] == "Page 14"


def test_successful_correction_transaction(original_claim):
    connection = MagicMock()
    cursor = connection.cursor.return_value

    # Original exists, has not been superseded,
    # and the replacement fingerprint is new.
    cursor.fetchone.side_effect = [
        original_claim,
        None,
        None,
    ]

    cursor.lastrowid = 999

    result = correct_claim(
        connection=connection,
        case_id=1,
        original_claim_id=100,
        new_value="48",
        new_location="Page 12",
        reason="Verified transcription correction",
    )

    assert result == {
        "original_claim_id": 100,
        "replacement_claim_id": 999,
    }

    connection.commit.assert_called_once()
    connection.rollback.assert_not_called()
    cursor.close.assert_called_once()


def test_reject_superseded_claim(original_claim):
    connection = MagicMock()
    cursor = connection.cursor.return_value

    cursor.fetchone.side_effect = [
        original_claim,
        {"replacement_claim_id": 999},
    ]

    with pytest.raises(ValueError, match="superseded"):
        correct_claim(
            connection,
            1,
            100,
            "48",
            "Page 12",
            "Correction",
        )

    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()


def test_reject_claim_outside_case():
    connection = MagicMock()
    cursor = connection.cursor.return_value

    cursor.fetchone.return_value = None

    with pytest.raises(ValueError, match="not found"):
        correct_claim(
            connection,
            1,
            9999,
            "48",
            "Page 12",
            "Correction",
        )

    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()


def test_reject_duplicate_replacement(original_claim):
    connection = MagicMock()
    cursor = connection.cursor.return_value

    cursor.fetchone.side_effect = [
        original_claim,
        None,
        {"claim_id": 888},
    ]

    with pytest.raises(ValueError, match="already exists"):
        correct_claim(
            connection,
            1,
            100,
            "48",
            "Page 12",
            "Correction",
        )

    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()


def test_rollback_on_insert_failure(original_claim):
    connection = MagicMock()
    cursor = connection.cursor.return_value

    cursor.fetchone.side_effect = [
        original_claim,
        None,
        None,
    ]

    def simulate_execute(sql, params):
        if "INSERT INTO disclosure_claims" in sql:
            raise RuntimeError("Simulated database failure")

    cursor.execute.side_effect = simulate_execute

    with pytest.raises(
        RuntimeError,
        match="Simulated database failure",
    ):
        correct_claim(
            connection,
            1,
            100,
            "48",
            "Page 12",
            "Correction",
        )

    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()

