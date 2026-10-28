
"""
Claim-correction lineage against the real test database
(corporate_intelligence_test only), with synthetic records.
"""

import threading

import pytest
from mysql.connector import errors

from src.claim_corrections import (
    LineageError,
    correct_claim,
    fetch_lineage,
    restore_claim,
)
from src.report_registry import register_run

# Guarded test-database helpers: identity-checked connections,
# synthetic fixtures, flagged cleanup, and the autouse guard
# against default (production) connections.
from test_mysql_evidence_snapshot import (  # noqa: F401
    block_default_connections,
    connect,
    pytestmark,
)
from test_mysql_report_registry import (  # noqa: F401
    currency,
    new_run,
    registry_db,
    seed,
)


LINEAGE_COLUMNS = {
    "correction_kind", "restored_claim_id", "evidence_source_id",
    "evidence_location", "submission_key",
}


@pytest.fixture
def lineage_db(registry_db):
    connection, fixtures, run_ids = registry_db
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = 'claim_corrections'"
        )
        present = {row[0] for row in cursor.fetchall()}
    finally:
        cursor.close()
        connection.rollback()

    if not LINEAGE_COLUMNS <= present:
        pytest.skip(
            "Claim lineage columns are not installed in the test database; "
            "apply sql/test_schema_upgrade_m22_4a.sql with a privileged "
            "account (test database only)."
        )

    case_id, a, b, c1, c2 = seed(fixtures)
    return connection, fixtures, run_ids, case_id, a, b, c1, c2


def current_claims(source_id):
    connection = connect()
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT claim_id, value_text, source_location "
            "FROM v_current_disclosure_claims WHERE source_id = %s",
            (source_id,),
        )
        rows = cursor.fetchall()
        cursor.close()
        connection.rollback()
        return rows
    finally:
        connection.close()


def claim_count(source_id):
    connection = connect()
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM disclosure_claims WHERE source_id = %s",
            (source_id,),
        )
        count = cursor.fetchone()[0]
        cursor.close()
        connection.rollback()
        return count
    finally:
        connection.close()


def lineage(connection, case_id, claim_id):
    result = fetch_lineage(connection, case_id, claim_id)
    connection.rollback()
    return result


def values(result):
    return [v["value_text"] for v in result["versions"]]


def test_single_correction(lineage_db):
    connection, _, _, case_id, _, b, _, c2 = lineage_db

    result = correct_claim(connection, case_id, c2, "51", "Page 4",
                           "Fictional transcription fix")

    replacement = result["replacement_claim_id"]
    assert current_claims(b) == [
        {"claim_id": replacement, "value_text": "51", "source_location": "Page 4"}
    ]
    chain = lineage(connection, case_id, c2)
    assert values(chain) == ["49", "51"]
    assert chain["active_claim_id"] == replacement
    (transition,) = chain["transitions"]
    assert transition["correction_kind"] == "CORRECTION"
    # Default evidence: the claim's own source at the new location.
    assert transition["evidence_source_id"] == b
    assert transition["evidence_location"] == "Page 4"
    assert chain["problems"] == []


def test_restoration_a_b_a(lineage_db):
    connection, _, _, case_id, _, b, _, c2 = lineage_db

    b_claim = correct_claim(connection, case_id, c2, "51", "Page 4",
                            "Fictional misreading")["replacement_claim_id"]
    restored = restore_claim(connection, case_id, b_claim, c2,
                             "Fictional: original value confirmed")
    a_again = restored["replacement_claim_id"]

    # The earlier claim is not reactivated; a new claim carries its value.
    assert a_again not in (c2, b_claim)
    assert current_claims(b) == [
        {"claim_id": a_again, "value_text": "49", "source_location": "Page 3"}
    ]

    chain = lineage(connection, case_id, a_again)
    assert values(chain) == ["49", "51", "49"]
    assert [t["correction_kind"] for t in chain["transitions"]] == [
        "CORRECTION", "RESTORATION",
    ]
    assert chain["transitions"][1]["restored_claim_id"] == c2
    # Distinct fingerprints, although the value repeats.
    fingerprints = [v["claim_fingerprint"] for v in chain["versions"]]
    assert len(set(fingerprints)) == 3
    assert chain["problems"] == []


def test_long_chain_a_b_c_d(lineage_db):
    connection, _, _, case_id, _, b, _, c2 = lineage_db

    current = c2
    for value, page in (("50", "Page 5"), ("51", "Page 6"), ("52", "Page 7")):
        current = correct_claim(connection, case_id, current, value, page,
                                f"Fictional step to {value}")["replacement_claim_id"]

    # Reconstruction from any version gives the same chain.
    for start in (c2, current):
        chain = lineage(connection, case_id, start)
        assert values(chain) == ["49", "50", "51", "52"]
        assert chain["root_claim_id"] == c2
        assert chain["active_claim_id"] == current
        assert [t["correction_reason"] for t in chain["transitions"]] == [
            "Fictional step to 50", "Fictional step to 51", "Fictional step to 52",
        ]
    assert [c["claim_id"] for c in current_claims(b)] == [current]


def test_duplicate_submission_is_idempotent(lineage_db):
    connection, _, _, case_id, _, b, _, c2 = lineage_db
    args = (connection, case_id, c2, "51", "Page 4", "Fictional fix")

    first = correct_claim(*args)
    before = claim_count(b)
    second = correct_claim(*args)

    assert second == first
    assert claim_count(b) == before
    assert len(lineage(connection, case_id, c2)["transitions"]) == 1

    # A different correction of the same, now superseded, claim is refused.
    with pytest.raises(LineageError, match="already been superseded"):
        correct_claim(connection, case_id, c2, "52", "Page 4", "Fictional fix")
    assert not connection.in_transaction


def test_correcting_superseded_claim_refused(lineage_db):
    connection, _, _, case_id, _, b, _, c2 = lineage_db
    correct_claim(connection, case_id, c2, "51", "Page 4", "Fictional fix")

    with pytest.raises(LineageError, match="current replacement"):
        restore_claim(connection, case_id, c2, c2, "Fictional")
    with pytest.raises(LineageError, match="current replacement"):
        correct_claim(connection, case_id, c2, "48", "Page 4", "Other fix")
    assert len(current_claims(b)) == 1


def test_concurrent_corrections_single_winner(lineage_db):
    connection, _, _, case_id, _, b, _, c2 = lineage_db
    barrier = threading.Barrier(2)
    outcomes = []

    def attempt(value):
        own = connect()
        try:
            barrier.wait()
            outcomes.append(correct_claim(own, case_id, c2, value, "Page 4",
                                          f"Fictional concurrent {value}"))
        except LineageError as error:
            outcomes.append(error)
        finally:
            own.close()

    threads = [threading.Thread(target=attempt, args=(v,)) for v in ("50", "51")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    winners = [o for o in outcomes if isinstance(o, dict)]
    losers = [o for o in outcomes if isinstance(o, LineageError)]
    assert len(winners) == 1 and len(losers) == 1
    (active,) = current_claims(b)
    assert active["claim_id"] == winners[0]["replacement_claim_id"]
    assert lineage(connection, case_id, c2)["problems"] == []


def test_supporting_evidence_rules(lineage_db):
    connection, fixtures, _, case_id, a, b, _, c2 = lineage_db

    # Blank evidence location or reason: nothing saved.
    with pytest.raises(ValueError, match="evidence location is required"):
        correct_claim(connection, case_id, c2, "51", "Page 4", "Fictional",
                      evidence_source_id=a, evidence_location="  ")
    with pytest.raises(ValueError, match="reason is required"):
        correct_claim(connection, case_id, c2, "51", "Page 4", " ")

    # Evidence from a source in a different investigation case.
    other_case = connect()
    try:
        cursor = other_case.cursor()
        cursor.execute("INSERT INTO investigation_cases (case_name) VALUES ('Other')")
        other_case_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO case_sources (case_id, document_title) VALUES (%s, 'Other')",
            (other_case_id,),
        )
        foreign_source = cursor.lastrowid
        other_case.commit()
        cursor.close()
    finally:
        other_case.close()
    fixtures.source_ids.append(foreign_source)
    try:
        with pytest.raises(ValueError, match="not part of this investigation"):
            correct_claim(connection, case_id, c2, "51", "Page 4", "Fictional",
                          evidence_source_id=foreign_source,
                          evidence_location="Page 1")
        assert claim_count(b) == 1

        # Evidence from another source in the same case is recorded.
        result = correct_claim(connection, case_id, c2, "51", "Page 4",
                               "Fictional: confirmed by second report",
                               evidence_source_id=a, evidence_location="Note 7")
        (transition,) = lineage(connection, case_id, c2)["transitions"]
        assert (transition["evidence_source_id"], transition["evidence_location"]) == (a, "Note 7")
        assert transition["replacement_claim_id"] == result["replacement_claim_id"]
    finally:
        cleanup = connect()
        try:
            cursor = cleanup.cursor()
            cursor.execute("DELETE FROM case_sources WHERE source_id = %s", (foreign_source,))
            cursor.execute("DELETE FROM investigation_cases WHERE case_id = %s", (other_case_id,))
            cleanup.commit()
            cursor.close()
        finally:
            cleanup.close()
        fixtures.source_ids.remove(foreign_source)


class FailingCursor:
    """Fails the second insert, after the replacement claim was inserted."""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=None):
        if "INSERT INTO claim_corrections" in sql:
            raise RuntimeError("Simulated failure halfway through")
        return self._cursor.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class FailingConnection:
    def __init__(self, connection):
        self._connection = connection

    def cursor(self, *args, **kwargs):
        return FailingCursor(self._connection.cursor(*args, **kwargs))

    def __getattr__(self, name):
        return getattr(self._connection, name)


def test_failure_halfway_rolls_back_everything(lineage_db):
    connection, _, _, case_id, _, b, _, c2 = lineage_db
    before = claim_count(b)

    with pytest.raises(RuntimeError, match="halfway"):
        correct_claim(FailingConnection(connection), case_id, c2, "51",
                      "Page 4", "Fictional fix")

    assert not connection.in_transaction
    assert claim_count(b) == before
    assert [c["claim_id"] for c in current_claims(b)] == [c2]


def test_invalid_restoration_targets(lineage_db):
    connection, _, _, case_id, _, _, c1, c2 = lineage_db
    b_claim = correct_claim(connection, case_id, c2, "51", "Page 4",
                            "Fictional fix")["replacement_claim_id"]

    # A claim from another lineage, or the active claim itself.
    for target in (c1, b_claim):
        with pytest.raises(LineageError, match="earlier version"):
            restore_claim(connection, case_id, b_claim, target, "Fictional")
    with pytest.raises(ValueError, match="not found"):
        restore_claim(connection, case_id + 1000000, b_claim, c2, "Fictional")
    assert not connection.in_transaction


def test_append_only_enforced_by_database(lineage_db):
    connection, _, _, case_id, _, _, _, c2 = lineage_db
    correct_claim(connection, case_id, c2, "51", "Page 4", "Fictional fix")
    cursor = connection.cursor()
    try:
        for statement in (
            "UPDATE claim_corrections SET correction_reason = 'x' "
            "WHERE original_claim_id = %s",
            "DELETE FROM claim_corrections WHERE original_claim_id = %s",
            "UPDATE disclosure_claims SET value_text = '1' WHERE claim_id = %s",
        ):
            with pytest.raises(errors.DatabaseError) as raised:
                cursor.execute(statement, (c2,))
            assert raised.value.errno == 1644
            connection.rollback()
    finally:
        cursor.close()


def test_reports_stale_not_corrupted_after_corrections(lineage_db, tmp_path):
    connection, _, run_ids, case_id, _, b, _, c2 = lineage_db

    run_id = "synthetic-lineage-report"
    run_ids.append(run_id)
    run_dir = new_run(tmp_path, case_id, run_id)
    files = {p.name: p.read_bytes() for p in run_dir.iterdir()}
    register_run(connection, case_id, run_id, tmp_path)
    assert currency(case_id, run_id, tmp_path)["currency"]["status"] == "CURRENT"

    b_claim = correct_claim(connection, case_id, c2, "51", "Page 4",
                            "Fictional fix")["replacement_claim_id"]
    a_again = restore_claim(connection, case_id, b_claim, c2,
                            "Fictional restoration")["replacement_claim_id"]

    result = currency(case_id, run_id, tmp_path)
    # Evidence changed: STALE. Files and hashes untouched: INTACT.
    assert result["integrity"]["status"] == "INTACT"
    assert result["currency"]["status"] == "STALE"
    changes = result["currency"]["changes"]
    assert changes["claims_removed"] == [c2]
    assert changes["claims_added"] == [a_again]
    assert len(changes["corrections_added"]) == 2
    assert {p.name: p.read_bytes() for p in run_dir.iterdir()} == files

    # The registration still references the original claim, which
    # remains in disclosure_claims as a historical version.
    chain = lineage(connection, case_id, c2)
    assert [v["claim_id"] for v in chain["versions"]] == [c2, b_claim, a_again]

    # A report made now reflects the restored state and is CURRENT.
    later = "synthetic-lineage-report-2"
    run_ids.append(later)
    new_run(tmp_path, case_id, later)
    register_run(connection, case_id, later, tmp_path)
    assert currency(case_id, later, tmp_path)["currency"]["status"] == "CURRENT"


def test_historical_versions_remain_retrievable(lineage_db):
    connection, _, _, case_id, _, b, _, c2 = lineage_db
    b_claim = correct_claim(connection, case_id, c2, "51", "Page 4",
                            "Fictional fix")["replacement_claim_id"]

    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT claim_id, value_text FROM disclosure_claims "
            "WHERE source_id = %s ORDER BY claim_id", (b,),
        )
        assert cursor.fetchall() == [
            {"claim_id": c2, "value_text": "49"},
            {"claim_id": b_claim, "value_text": "51"},
        ]
    finally:
        cursor.close()
        connection.rollback()
    assert [c["claim_id"] for c in current_claims(b)] == [b_claim]


def test_caller_transaction_is_left_alone(lineage_db):
    connection, _, _, case_id, _, b, _, c2 = lineage_db
    cursor = connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM disclosure_claims WHERE source_id = %s", (b,))
    cursor.fetchall()
    cursor.close()
    assert connection.in_transaction

    with pytest.raises(ValueError, match="already has an open transaction"):
        correct_claim(connection, case_id, c2, "51", "Page 4", "Fictional fix")

    # Still the caller's transaction: neither committed nor rolled back.
    assert connection.in_transaction
    connection.rollback()
    assert [c["claim_id"] for c in current_claims(b)] == [c2]
