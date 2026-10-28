
import hashlib
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from mysql.connector import errors

from src.claim_corrections import (
    LineageError,
    build_lineage,
    claim_fingerprint,
    correct_claim,
    prepare_replacement,
    restore_claim,
    submission_key,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def claim():
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


def version(claim_id, value, location="Page 12"):
    return {"claim_id": claim_id, "value_text": value, "source_location": location}


def transition(correction_id, original, replacement, kind="CORRECTION", restored=None):
    return {
        "correction_id": correction_id, "original_claim_id": original,
        "replacement_claim_id": replacement, "correction_kind": kind,
        "restored_claim_id": restored,
    }


# Fingerprints

def test_original_fingerprint_matches_importer(claim):
    importer = hashlib.sha256(json.dumps(
        [200, "Example Corporation", "equity_interest_pct", 2025,
         "2025-12-31", "49", "Page 12"],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()

    assert claim_fingerprint(claim, "49", "Page 12") == importer


def test_replacement_fingerprints_distinguish_repeated_values(claim):
    original = claim_fingerprint(claim, "49", "Page 12")
    restored_after_100 = claim_fingerprint(claim, "49", "Page 12", supersedes=100)
    restored_after_101 = claim_fingerprint(claim, "49", "Page 12", supersedes=101)

    assert len({original, restored_after_100, restored_after_101}) == 3
    # Deterministic.
    assert restored_after_100 == claim_fingerprint(claim, "49", "Page 12", supersedes=100)


def test_prepare_replacement_records_lineage_and_evidence(claim):
    default = prepare_replacement(claim, "48", "Page 13", "Fix")
    assert default["claim_fingerprint"] == claim_fingerprint(
        claim, "48", "Page 13", supersedes=100)
    assert (default["evidence_source_id"], default["evidence_location"]) == (200, "Page 13")

    cited = prepare_replacement(claim, "48", "Page 13", "Fix",
                                evidence_source_id=201, evidence_location="Note 4")
    assert (cited["evidence_source_id"], cited["evidence_location"]) == (201, "Note 4")


@pytest.mark.parametrize("location", ["", "   "])
def test_blank_evidence_location_rejected(claim, location):
    with pytest.raises(ValueError, match="evidence location is required"):
        prepare_replacement(claim, "48", "Page 13", "Fix",
                            evidence_source_id=201, evidence_location=location)


def test_submission_key_is_stable_and_specific():
    base = (100, "CORRECTION", None, "48", "Page 13", "Fix", None, None)
    assert submission_key(*base) == submission_key(100, "CORRECTION", None,
                                                   " 48 ", "Page 13 ", "Fix", None, None)
    assert submission_key(*base) != submission_key(100, "CORRECTION", None,
                                                   "48", "Page 13", "Other reason", None, None)
    assert submission_key(*base) != submission_key(101, *base[1:])


# Transactions (mocked database)

def test_identical_resubmission_returns_existing_result(claim):
    connection = MagicMock()
    cursor = connection.cursor.return_value
    key = submission_key(100, "CORRECTION", None, "48", "Page 13", "Fix", None, None)
    cursor.fetchone.side_effect = [
        claim, {"replacement_claim_id": 999, "submission_key": key},
    ]

    result = correct_claim(connection, 1, 100, "48", "Page 13", "Fix")

    assert result == {"original_claim_id": 100, "replacement_claim_id": 999}
    connection.commit.assert_not_called()
    connection.rollback.assert_called_once()
    inserts = [c for c in cursor.execute.call_args_list if "INSERT" in c.args[0]]
    assert inserts == []


def test_evidence_source_outside_case_rejected(claim):
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.fetchone.side_effect = [claim, None, None]

    with pytest.raises(ValueError, match="not part of this investigation"):
        correct_claim(connection, 1, 100, "48", "Page 13", "Fix",
                      evidence_source_id=999, evidence_location="Page 1")

    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()


def test_restoration_requires_earlier_version(claim):
    connection = MagicMock()
    cursor = connection.cursor.return_value
    # Claim exists, not superseded, and has no predecessor.
    cursor.fetchone.side_effect = [claim, None, None]

    with pytest.raises(LineageError, match="earlier version"):
        restore_claim(connection, 1, 100, 50, "Fix")

    connection.commit.assert_not_called()
    with pytest.raises(ValueError, match="Select the earlier version"):
        restore_claim(connection, 1, 100, None, "Fix")


@pytest.mark.parametrize("errno, expected, message", [
    (1054, RuntimeError, "lineage schema"),
    (1062, LineageError, "conflicting correction"),
])
def test_database_errors_reported_without_partial_writes(claim, errno, expected, message):
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.fetchone.side_effect = [claim, None, None]

    def execute(sql, params=None):
        if "INSERT INTO claim_corrections" in sql:
            raise errors.DatabaseError(msg="simulated", errno=errno)

    cursor.execute.side_effect = execute

    with pytest.raises(expected, match=message):
        correct_claim(connection, 1, 100, "48", "Page 13", "Fix")

    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()


def test_caller_transaction_refused_and_untouched(claim):
    connection = MagicMock()
    connection.in_transaction = True

    with pytest.raises(ValueError, match="already has an open transaction"):
        correct_claim(connection, 1, 100, "48", "Page 13", "Fix")

    connection.cursor.assert_not_called()
    connection.rollback.assert_not_called()
    connection.commit.assert_not_called()


# Lineage reconstruction (pure)

def test_reconstructs_restoration_chain():
    claims = [version(1, "49"), version(2, "51"), version(3, "49")]
    corrections = [transition(10, 1, 2), transition(11, 2, 3, "RESTORATION", 1)]

    for start in (1, 2, 3):
        result = build_lineage(claims, corrections, start)
        assert [v["claim_id"] for v in result["versions"]] == [1, 2, 3]
        assert (result["root_claim_id"], result["active_claim_id"]) == (1, 3)
        assert result["problems"] == []


@pytest.mark.parametrize("corrections, problem", [
    ([transition(10, 1, 2), transition(11, 1, 3)], "more than one replacement"),
    ([transition(10, 2, 1)], "runs backwards"),
    ([transition(10, 1, 2), transition(11, 2, 1)], "cycle"),
    ([transition(10, 1, 2), transition(11, 2, 3, "RESTORATION", 3)],
     "outside the earlier lineage"),
    ([transition(10, 1, 2), transition(11, 2, 3, "RESTORATION", None)],
     "outside the earlier lineage"),
])
def test_detects_impossible_lineage_states(corrections, problem):
    claims = [version(1, "49"), version(2, "51"), version(3, "49")]

    result = build_lineage(claims, corrections, 2)

    assert any(problem in p for p in result["problems"]), result["problems"]


def test_detects_restoration_with_wrong_value():
    claims = [version(1, "49"), version(2, "51"), version(3, "50")]
    corrections = [transition(10, 1, 2), transition(11, 2, 3, "RESTORATION", 1)]

    result = build_lineage(claims, corrections, 3)

    assert any("does not restore the value" in p for p in result["problems"])


def test_unknown_claim_rejected():
    with pytest.raises(ValueError, match="not in this lineage"):
        build_lineage([version(1, "49")], [], 2)


# Migrations

def _alter(path, database):
    text = (ROOT / path).read_text()
    start = re.search(r"^ALTER TABLE", text, re.MULTILINE).start()
    body = text[start:text.index(";", start)]
    return body.replace(f"{database}.", "<db>.")


def test_test_migration_matches_production():
    production = _alter("sql/migrations/m22_4a_claim_correction_lineage.sql",
                        "corporate_intelligence")
    test = _alter("sql/test_schema_upgrade_m22_4a.sql", "corporate_intelligence_test")

    assert production == test


def test_production_migration_is_additive_and_qualified():
    text = (ROOT / "sql/migrations/m22_4a_claim_correction_lineage.sql").read_text()
    code = "\n".join(line for line in text.splitlines() if not line.startswith("--"))

    for forbidden in ("DROP ", "DELETE FROM", "UPDATE corporate", "INSERT ", "GRANT ",
                      "corporate_intelligence_test", "USE "):
        assert forbidden not in code
    names = re.findall(r"\b(?:TABLE|REFERENCES|ON|TRIGGER)\s+([A-Za-z_][\w.]*)", code)
    assert names
    assert all(n.startswith("corporate_intelligence.") for n in names), names
    assert code.count("CREATE TRIGGER") == 3
    # Production deletions are never permitted, not even by a flag.
    assert "maintenance" not in code
