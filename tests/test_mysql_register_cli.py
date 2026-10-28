
import io
import json

import pytest

import src.register_report_run as cli
from src.claim_corrections import correct_claim
from src.report_registry import fetch_registration

# Guarded test-database helpers: identity-checked connections,
# synthetic fixtures, flagged cleanup of registry rows, and the
# autouse guard against default (production) connections.
from test_mysql_evidence_snapshot import (  # noqa: F401
    block_default_connections,
    connect,
    pytestmark,
)
from test_mysql_report_registry import (  # noqa: F401
    new_run,
    registry_db,
    seed,
    snapshot_now,
)


TEST_DB = "corporate_intelligence_test"


def cli_run(case_id, run_id, runs_root, *extra):
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(
        ["--case-id", str(case_id), "--run-id", run_id,
         "--database", TEST_DB, "--runs-root", str(runs_root), *extra],
        stdout=out, stderr=err,
    )
    return code, out.getvalue() + err.getvalue()


def registry_counts():
    connection = connect()
    try:
        cursor = connection.cursor()
        counts = []
        for table in ("report_runs", "report_run_claims", "report_run_sources"):
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            counts.append(cursor.fetchone()[0])
        cursor.close()
        connection.rollback()
        return counts
    finally:
        connection.close()


@pytest.fixture
def tracked_connections(monkeypatch):
    """Record each CLI connection's transaction state when closed."""

    states = []
    real = cli.connect_to

    def tracking(database):
        connection = real(database)
        close = connection.close

        def close_and_record():
            states.append((database, connection.in_transaction))
            close()

        connection.close = close_and_record
        return connection

    monkeypatch.setattr(cli, "connect_to", tracking)
    return states


def test_cli_registration_lifecycle(registry_db, tmp_path, tracked_connections,
                                    monkeypatch):
    connection, fixtures, run_ids = registry_db
    case_id, *_ = seed(fixtures)

    # A misleading environment default must not redirect writes.
    monkeypatch.setenv("MYSQL_DATABASE", "corporate_intelligence")

    run_id = "synthetic-cli"
    run_ids.append(run_id)
    run_dir = new_run(tmp_path, case_id, run_id)
    files = {p.name: p.read_bytes() for p in run_dir.iterdir()}
    before = registry_counts()

    # Dry run: ready, zero changes.
    code, text = cli_run(case_id, run_id, tmp_path)
    assert code == 0, text
    assert "Database: corporate_intelligence_test (identity confirmed)" in text
    assert "READY TO REGISTER" in text
    assert registry_counts() == before
    assert fetch_registration(connection, run_id) is None
    connection.rollback()

    # Commit: registered in the test database.
    code, text = cli_run(case_id, run_id, tmp_path, "--commit")
    assert code == 0, text
    assert "Result: REGISTERED" in text
    stored = fetch_registration(connection, run_id)
    connection.rollback()
    assert stored["run"]["run_path"] == f"case_{case_id}/{run_id}"
    assert registry_counts() == [before[0] + 1, before[1] + 2, before[2] + 2]

    # Repeat: idempotent, nothing written.
    for extra in ((), ("--commit",)):
        code, text = cli_run(case_id, run_id, tmp_path, *extra)
        assert code == 0, text
        assert "ALREADY REGISTERED" in text
    assert registry_counts() == [before[0] + 1, before[1] + 2, before[2] + 2]

    # Report artifacts were only read.
    assert {p.name: p.read_bytes() for p in run_dir.iterdir()} == files

    # Every CLI connection was closed with no transaction open.
    assert tracked_connections
    assert all(db == TEST_DB and not open_tx for db, open_tx in tracked_connections)


def test_cli_conflict_fails_safely(registry_db, tmp_path):
    connection, fixtures, run_ids = registry_db
    case_id, a, *_ = seed(fixtures)

    run_id = "synthetic-cli-conflict"
    run_ids.append(run_id)
    new_run(tmp_path / "original", case_id, run_id)
    assert cli_run(case_id, run_id, tmp_path / "original", "--commit")[0] == 0
    before = fetch_registration(connection, run_id)
    connection.rollback()

    fixtures.claim(a, 2025, "60")
    connection.commit()
    new_run(tmp_path / "other", case_id, run_id)

    for extra in ((), ("--commit",)):
        code, text = cli_run(case_id, run_id, tmp_path / "other", *extra)
        assert code == cli.EXIT_CONFLICT, text
        assert "evidence_snapshot_sha256" in text

    assert fetch_registration(connection, run_id) == before
    connection.rollback()


@pytest.mark.parametrize("damage", ["missing_report", "missing_manifest",
                                    "malformed_manifest"])
def test_cli_damaged_run_fails_before_database(registry_db, tmp_path, damage):
    connection, fixtures, run_ids = registry_db
    case_id, *_ = seed(fixtures)

    run_id = f"synthetic-cli-{damage.replace('_', '-')}"
    run_ids.append(run_id)
    run_dir = new_run(tmp_path, case_id, run_id)
    before = registry_counts()

    if damage == "missing_report":
        (run_dir / "report.md").unlink()
    elif damage == "missing_manifest":
        (run_dir / "manifest.json").unlink()
    else:
        (run_dir / "manifest.json").chmod(0o644)
        (run_dir / "manifest.json").write_text("{not json")

    code, text = cli_run(case_id, run_id, tmp_path, "--commit")

    assert code == cli.EXIT_FILES, text
    assert "File validation: FAILED" in text
    assert registry_counts() == before


def test_cli_evidence_mismatch_leaves_no_partial_records(registry_db, tmp_path):
    connection, fixtures, run_ids = registry_db
    case_id, *_ = seed(fixtures)

    tampered = json.loads(json.dumps(snapshot_now(case_id)))
    tampered["claims"][1]["claim_fingerprint"] = "0" * 64

    run_id = "synthetic-cli-mismatch"
    run_ids.append(run_id)
    new_run(tmp_path, case_id, run_id, snapshot=tampered)
    before = registry_counts()

    code, text = cli_run(case_id, run_id, tmp_path, "--commit")

    assert code == cli.EXIT_EVIDENCE, text
    assert registry_counts() == before
    assert fetch_registration(connection, run_id) is None
    connection.rollback()


def test_stale_historical_run_still_registers(registry_db, tmp_path):
    """
    A run is registered against the evidence it captured; later
    corrections make it STALE, not unregistrable, because
    superseded claims remain in disclosure_claims.
    """

    connection, fixtures, run_ids = registry_db
    case_id, a, b, c1, c2 = seed(fixtures)

    run_id = "synthetic-cli-stale"
    run_ids.append(run_id)
    new_run(tmp_path, case_id, run_id)

    correct_claim(connection, case_id, c2, "49", "Page 7", "Fictional fix")

    code, text = cli_run(case_id, run_id, tmp_path, "--commit")

    assert code == 0, text
    assert "Result: REGISTERED" in text
    stored = fetch_registration(connection, run_id)
    connection.rollback()
    # The superseded claim stays referenced exactly as captured.
    assert c2 in [claim[0] for claim in stored["claims"]]


def test_cli_cannot_reach_production(monkeypatch, tmp_path):
    """Only the two named databases are accepted, and identity is checked."""

    with pytest.raises(cli.DatabaseTargetError):
        cli.connect_to("mysql")

    connection = cli.connect_to(TEST_DB)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT DATABASE()")
        assert cursor.fetchone()[0] == TEST_DB
        cursor.close()
        connection.rollback()
    finally:
        connection.close()
