
import json
import shutil
import threading

import pytest
from mysql.connector import errors

from src.claim_corrections import correct_claim
from src.evidence_snapshot import load_evidence_snapshot
from src.generate_versioned_report import generate_versioned_report
from src.report_registry import (
    RegistrationConflict,
    RegistryError,
    fetch_registration,
    list_registered_runs,
    register_run,
    registry_installed,
)
from src.report_verification import capture_current_snapshot, verify_registered_run

# Shared test-database helpers, including the autouse guard that
# fails if any code path falls back to the production connection.
from test_mysql_evidence_snapshot import (  # noqa: F401
    Fixtures,
    block_default_connections,
    connect,
    pytestmark,
)


CODE = {"git_commit": None, "working_tree_dirty": None}


def snapshot_now(case_id):
    reader = connect()
    try:
        reader.start_transaction(
            consistent_snapshot=True,
            isolation_level="REPEATABLE READ",
            readonly=True,
        )
        snapshot = load_evidence_snapshot(
            case_id=case_id, years=(2024, 2025), connection=reader
        )
        reader.rollback()
        return snapshot
    finally:
        reader.close()


def new_run(root, case_id, run_id, snapshot=None):
    return generate_versioned_report(
        case_id=case_id,
        output_root=root,
        snapshot=snapshot or snapshot_now(case_id),
        run_id=run_id,
        code_version=CODE,
    )["run_dir"]


def currency(case_id, run_id, runs_root):
    connection = connect()
    try:
        stored = fetch_registration(connection, run_id)
        connection.rollback()
    finally:
        connection.close()

    return verify_registered_run(
        stored,
        load_current=lambda case, years: capture_current_snapshot(
            connect, case, years
        ),
        runs_root=runs_root,
    )


def review(cursor, source_id, previous, new):
    """Record an assessment change exactly as the audit trail requires."""

    cursor.execute(
        """
        INSERT INTO source_assessment_history (
            source_id, event_type, previous_assessment,
            new_assessment, review_reason
        )
        VALUES (%s, 'REVIEW', %s, %s, 'Fictional review')
        """,
        (source_id, previous, new),
    )
    cursor.execute(
        "UPDATE case_sources SET evidence_assessment = %s "
        "WHERE source_id = %s",
        (new, source_id),
    )


@pytest.fixture
def registry_db():
    connection = connect()

    installed = registry_installed(connection)

    # The information_schema check opened an implicit read
    # transaction; end it so tests receive a clean connection.
    connection.rollback()
    assert not connection.in_transaction

    if not installed:
        connection.close()
        pytest.skip(
            "Registry tables are not installed in the test database; "
            "apply sql/test_report_registry_m22_3b.sql with a "
            "privileged account (test database only)."
        )

    fixtures = Fixtures(connection)
    run_ids = []

    yield connection, fixtures, run_ids

    connection.rollback()
    cursor = connection.cursor()
    try:
        if run_ids:
            slots = ", ".join(["%s"] * len(run_ids))
            # Test database only: deletion requires this explicit flag.
            cursor.execute("SET @crie_registry_maintenance = 1")
            for table in ("report_run_claims", "report_run_sources", "report_runs"):
                cursor.execute(
                    f"DELETE FROM {table} WHERE run_id IN ({slots})", run_ids
                )
            cursor.execute("SET @crie_registry_maintenance = 0")
            connection.commit()
    finally:
        cursor.close()
        try:
            fixtures.cleanup()
        finally:
            connection.close()


def seed(fixtures):
    case_id = fixtures.case()
    a = fixtures.source(evidence_assessment="SUPPORTS")
    b = fixtures.source(evidence_assessment="SUPPORTS")

    cursor = fixtures.connection.cursor()
    try:
        for source_id in (a, b):
            cursor.execute(
                """
                INSERT INTO source_assessment_history (
                    source_id, event_type, previous_assessment,
                    new_assessment, review_reason
                )
                VALUES (%s, 'REVIEW', 'NOT_REVIEWED', 'SUPPORTS', 'Fictional')
                """,
                (source_id,),
            )
    finally:
        cursor.close()

    c1 = fixtures.claim(a, 2024, "49")
    c2 = fixtures.claim(b, 2025, "49")
    fixtures.connection.commit()

    return case_id, a, b, c1, c2


def test_registration_lifecycle(registry_db, tmp_path):
    connection, fixtures, run_ids = registry_db
    case_id, a, b, c1, c2 = seed(fixtures)

    first = "synthetic-first"
    run_ids.append(first)
    run_dir = new_run(tmp_path, case_id, first)
    files_before = {p.name: p.read_bytes() for p in run_dir.iterdir()}

    # Register, then register again (idempotent).
    assert register_run(connection, case_id, first, tmp_path)["status"] == "REGISTERED"
    assert register_run(
        connection, case_id, first, tmp_path
    )["status"] == "ALREADY_REGISTERED"

    stored = fetch_registration(connection, first)
    connection.rollback()
    assert [c[0] for c in stored["claims"]] == [c1, c2]
    assert [s[0] for s in stored["sources"]] == [a, b]
    assert stored["run"]["run_path"] == f"case_{case_id}/{first}"

    listed = list_registered_runs(connection, case_id)
    connection.rollback()
    assert [r["run_id"] for r in listed] == [first]

    # Registration leaves the original files untouched.
    assert {p.name: p.read_bytes() for p in run_dir.iterdir()} == files_before

    # Evidence unchanged: CURRENT.
    result = currency(case_id, first, tmp_path)
    assert result["integrity"]["status"] == "INTACT"
    assert result["currency"]["status"] == "CURRENT"

    # New evidence: STALE.
    c3 = fixtures.claim(a, 2025, "49")
    connection.commit()
    result = currency(case_id, first, tmp_path)
    assert result["currency"]["status"] == "STALE"
    assert result["currency"]["changes"] == {"claims_added": [c3]}

    # A later run reflects the new evidence and is CURRENT.
    second = "synthetic-second"
    run_ids.append(second)
    new_run(tmp_path, case_id, second)
    register_run(connection, case_id, second, tmp_path)
    assert currency(case_id, second, tmp_path)["currency"]["status"] == "CURRENT"

    # An analyst correction makes it STALE.
    correction = correct_claim(
        connection, case_id, c2, "49", "Page 9", "Fictional location fix"
    )
    changes = currency(case_id, second, tmp_path)["currency"]
    assert changes["status"] == "STALE"
    assert changes["changes"]["claims_removed"] == [c2]
    assert changes["changes"]["claims_added"] == [
        correction["replacement_claim_id"]
    ]
    assert len(changes["changes"]["corrections_added"]) == 1

    # A source re-assessment makes a fresh run STALE.
    third = "synthetic-third"
    run_ids.append(third)
    new_run(tmp_path, case_id, third)
    register_run(connection, case_id, third, tmp_path)
    cursor = connection.cursor()
    review(cursor, b, "SUPPORTS", "INCONCLUSIVE")
    cursor.close()
    connection.commit()
    changes = currency(case_id, third, tmp_path)["currency"]
    assert changes["status"] == "STALE"
    assert changes["changes"] == {"sources_reassessed": [b]}

    # Deleted historical run: integrity MISSING, registry kept,
    # currency still computed from the registered hash.
    shutil.rmtree(run_dir)
    result = currency(case_id, first, tmp_path)
    assert result["integrity"]["status"] == "MISSING"
    assert result["currency"]["status"] == "STALE"
    assert fetch_registration(connection, first) is not None
    connection.rollback()


def test_append_only_triggers(registry_db, tmp_path):
    connection, fixtures, run_ids = registry_db
    case_id, *_ = seed(fixtures)

    run_ids.append("synthetic-immutable")
    new_run(tmp_path, case_id, "synthetic-immutable")
    register_run(connection, case_id, "synthetic-immutable", tmp_path)

    cursor = connection.cursor()
    try:
        for statement in (
            "UPDATE report_runs SET report_bytes = 1 WHERE run_id = %s",
            "UPDATE report_run_claims SET source_id = source_id WHERE run_id = %s",
            "DELETE FROM report_runs WHERE run_id = %s",
            "DELETE FROM report_run_sources WHERE run_id = %s",
        ):
            with pytest.raises(errors.DatabaseError, match="append-only"):
                cursor.execute(statement, ("synthetic-immutable",))
            connection.rollback()
    finally:
        cursor.close()

    assert fetch_registration(connection, "synthetic-immutable") is not None
    connection.rollback()


def test_conflicting_registration_changes_nothing(registry_db, tmp_path):
    connection, fixtures, run_ids = registry_db
    case_id, a, b, c1, c2 = seed(fixtures)

    run_id = "synthetic-conflict"
    run_ids.append(run_id)
    new_run(tmp_path / "original", case_id, run_id)
    register_run(connection, case_id, run_id, tmp_path / "original")
    before = fetch_registration(connection, run_id)
    connection.rollback()

    # Same run ID, different evidence in another runs root.
    fixtures.claim(a, 2025, "60")
    connection.commit()
    new_run(tmp_path / "other", case_id, run_id)

    with pytest.raises(RegistrationConflict, match="evidence_snapshot_sha256"):
        register_run(connection, case_id, run_id, tmp_path / "other")

    assert fetch_registration(connection, run_id) == before
    connection.rollback()


def test_failed_registration_rolls_back(registry_db, tmp_path):
    connection, fixtures, run_ids = registry_db
    case_id, *_ = seed(fixtures)

    # A self-consistent run whose claim fingerprint does not match
    # the registered evidence: rejected before anything is stored.
    snapshot = snapshot_now(case_id)
    tampered = json.loads(json.dumps(snapshot))
    tampered["claims"][0]["claim_fingerprint"] = "0" * 64

    run_id = "synthetic-rejected"
    run_ids.append(run_id)
    new_run(tmp_path, case_id, run_id, snapshot=tampered)

    with pytest.raises(RegistryError, match="do not match registered claims"):
        register_run(connection, case_id, run_id, tmp_path)

    assert fetch_registration(connection, run_id) is None
    connection.rollback()


def test_concurrent_registration_single_winner(registry_db, tmp_path):
    connection, fixtures, run_ids = registry_db
    case_id, *_ = seed(fixtures)

    run_id = "synthetic-concurrent"
    run_ids.append(run_id)
    new_run(tmp_path, case_id, run_id)

    results, failures = [], []
    barrier = threading.Barrier(2)

    def register():
        own = connect()
        try:
            barrier.wait()
            results.append(register_run(own, case_id, run_id, tmp_path)["status"])
        except Exception as error:
            failures.append(error)
        finally:
            own.close()

    threads = [threading.Thread(target=register) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert failures == []
    assert sorted(results) == ["ALREADY_REGISTERED", "REGISTERED"]

    stored = fetch_registration(connection, run_id)
    connection.rollback()
    assert len(stored["claims"]) == 2


def test_unavailable_database_is_unknown(registry_db, tmp_path):
    connection, fixtures, run_ids = registry_db
    case_id, *_ = seed(fixtures)

    run_id = "synthetic-offline"
    run_ids.append(run_id)
    new_run(tmp_path, case_id, run_id)
    register_run(connection, case_id, run_id, tmp_path)
    stored = fetch_registration(connection, run_id)
    connection.rollback()

    def offline():
        raise errors.InterfaceError("Can't connect to MySQL server")

    result = verify_registered_run(
        stored,
        load_current=lambda case, years: capture_current_snapshot(
            offline, case, years
        ),
        runs_root=tmp_path,
    )

    assert result["integrity"]["status"] == "INTACT"
    assert result["currency"]["status"] == "UNKNOWN"


def _visible_elsewhere(claim_id):
    """Whether another session can see a claim (i.e. it was committed)."""

    other = connect()
    try:
        cursor = other.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM disclosure_claims WHERE claim_id = %s",
            (claim_id,),
        )
        visible = cursor.fetchone()[0] == 1
        cursor.close()
        other.rollback()
        return visible
    finally:
        other.close()


@pytest.mark.parametrize("ownership", ["implicit", "explicit"])
def test_caller_transaction_refused_and_never_committed(
    registry_db, tmp_path, ownership
):
    connection, fixtures, run_ids = registry_db
    case_id, a, *_ = seed(fixtures)

    run_id = f"synthetic-caller-{ownership}"
    run_ids.append(run_id)
    new_run(tmp_path, case_id, run_id)

    # Caller work left uncommitted on the same connection.
    if ownership == "explicit":
        connection.start_transaction()
    else:
        cursor = connection.cursor()
        cursor.execute("SELECT COUNT(*) FROM case_sources")
        cursor.fetchone()
        cursor.close()
    pending = fixtures.claim(a, 2025, "51")
    assert connection.in_transaction

    with pytest.raises(ValueError, match="open transaction"):
        register_run(connection, case_id, run_id, tmp_path)

    # register_run neither committed nor rolled back the caller's work.
    assert connection.in_transaction
    assert not _visible_elsewhere(pending)

    connection.rollback()
    assert not _visible_elsewhere(pending)
    assert fetch_registration(connection, run_id) is None
    connection.rollback()

    # With the caller's transaction ended, registration succeeds.
    assert register_run(connection, case_id, run_id, tmp_path)["status"] == "REGISTERED"
    assert not connection.in_transaction


def test_connection_clean_after_each_outcome(registry_db, tmp_path):
    connection, fixtures, run_ids = registry_db
    case_id, *_ = seed(fixtures)

    run_id = "synthetic-clean"
    run_ids.append(run_id)
    new_run(tmp_path, case_id, run_id)

    assert register_run(connection, case_id, run_id, tmp_path)["status"] == "REGISTERED"
    assert not connection.in_transaction

    assert register_run(
        connection, case_id, run_id, tmp_path
    )["status"] == "ALREADY_REGISTERED"
    assert not connection.in_transaction

    with pytest.raises(RegistryError):
        register_run(connection, case_id, "synthetic-absent", tmp_path)
    assert not connection.in_transaction
