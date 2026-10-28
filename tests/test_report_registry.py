
import datetime
import hashlib
import json
from unittest.mock import MagicMock

import pytest
from mysql.connector import errors

import src.report_registry as registry
from src.generate_versioned_report import generate_versioned_report
from src.report_registry import (
    ManifestError,
    RegistrationConflict,
    RegistryUnavailable,
    RunFilesError,
    RunIntegrityError,
    prepare_run,
    register_run,
    resolve_run_dir,
    run_path,
)


NOW = datetime.datetime(2026, 2, 1, 12, 0, tzinfo=datetime.timezone.utc)
CODE = {"git_commit": "a" * 40, "working_tree_dirty": False}
RUN_ID = "run-reg"


@pytest.fixture
def run(snapshot_tools, tmp_path):
    """A real versioned run written into a temporary runs root."""

    t = snapshot_tools
    snapshot = t.snapshot([
        t.claim(11, 1, "49", year=2024),
        t.claim(12, 2, "49", year=2025, location="Note 4"),
    ])
    result = generate_versioned_report(
        case_id=7, output_root=tmp_path, snapshot=snapshot,
        run_id=RUN_ID, now=NOW, code_version=CODE,
    )
    return {"root": tmp_path, "dir": result["run_dir"], "snapshot": snapshot}


def file_hashes(run_dir):
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in run_dir.iterdir()
    }


def rewrite(path, transform):
    path.chmod(0o644)
    path.write_bytes(transform(path.read_bytes()))


# --- Reading and validating stored runs ------------------------


def test_prepare_run_builds_registration(run):
    before = file_hashes(run["dir"])

    rows = prepare_run(7, RUN_ID, run["root"])

    record = rows["run"]
    assert record["run_id"] == RUN_ID
    assert record["case_id"] == 7
    assert (record["previous_year"], record["current_year"]) == (2024, 2025)
    assert record["generated_at_utc"] == datetime.datetime(2026, 2, 1, 12, 0)
    assert record["git_commit"] == "a" * 40
    assert record["working_tree_dirty"] == 0
    assert record["run_path"] == f"case_7/{RUN_ID}"
    assert record["manifest_sha256"] == before["manifest.json"]
    assert record["report_sha256"] == before["report.md"]
    assert (record["claim_count"], record["source_count"]) == (2, 2)

    assert rows["claims"] == [
        (11, f"{11:064x}", 1),
        (12, f"{12:064x}", 2),
    ]
    assert rows["sources"] == [(1, "SUPPORTS", 10), (2, "SUPPORTS", 20)]

    # Registration never regenerates or alters artifacts.
    assert file_hashes(run["dir"]) == before


@pytest.mark.parametrize(
    "case_id,run_id",
    [
        (7, "../escape"),
        (7, "a/../../b"),
        (7, ".hidden"),
        (7, ""),
        (7, "x" * 200),
        (7, None),
        ("7/..", RUN_ID),
        (0, RUN_ID),
        (-1, RUN_ID),
        (True, RUN_ID),
    ],
)
def test_path_traversal_rejected(run, case_id, run_id):
    with pytest.raises(ValueError):
        resolve_run_dir(case_id, run_id, run["root"])


def test_symlink_outside_root_refused(run, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (run["root"] / "case_7" / "linked").symlink_to(elsewhere)

    with pytest.raises(RunFilesError, match="escapes the runs root"):
        prepare_run(7, "linked", run["root"])


def test_symlink_inside_root_refused(run):
    # Even a link to a genuine run is not accepted as a run.
    (run["root"] / "case_7" / "alias").symlink_to(run["dir"])

    with pytest.raises(RunFilesError, match="symbolic link"):
        prepare_run(7, "alias", run["root"])


def test_symlinked_file_refused(run, tmp_path_factory):
    target = tmp_path_factory.mktemp("t") / "report.md"
    target.write_bytes((run["dir"] / "report.md").read_bytes())
    (run["dir"] / "report.md").unlink()
    (run["dir"] / "report.md").symlink_to(target)

    with pytest.raises(RunFilesError, match="not a regular file"):
        prepare_run(7, RUN_ID, run["root"])


def test_missing_run_directory(run):
    with pytest.raises(RunFilesError, match="not found"):
        prepare_run(7, "no-such-run", run["root"])


@pytest.mark.parametrize("name", ["report.md", "manifest.json"])
def test_missing_run_file(run, name):
    (run["dir"] / name).unlink()

    with pytest.raises(RunFilesError, match=f"Missing {name}"):
        prepare_run(7, RUN_ID, run["root"])


def test_altered_report_bytes(run):
    rewrite(run["dir"] / "report.md", lambda b: b.replace(b"49%", b"48%"))

    with pytest.raises(RunIntegrityError, match="report SHA-256"):
        prepare_run(7, RUN_ID, run["root"])


def test_altered_manifest_evidence(run):
    def edit(data):
        manifest = json.loads(data)
        manifest["evidence_snapshot"]["claims"][0]["value"] = "48"
        return json.dumps(manifest).encode()

    rewrite(run["dir"] / "manifest.json", edit)

    with pytest.raises(RunIntegrityError, match="evidence snapshot hash"):
        prepare_run(7, RUN_ID, run["root"])


def test_altered_manifest_analysis(run):
    def edit(data):
        manifest = json.loads(data)
        manifest["analysis"]["comparison"][0]["comparison_status"] = "CHANGED"
        return json.dumps(manifest).encode()

    rewrite(run["dir"] / "manifest.json", edit)

    with pytest.raises(RunIntegrityError, match="analysis hash"):
        prepare_run(7, RUN_ID, run["root"])


@pytest.mark.parametrize(
    "transform,message",
    [
        (lambda b: b[:-20], "not valid JSON"),
        (lambda b: b"\xff\xfe", "not valid JSON"),
        (lambda b: b"[]", "not a JSON object"),
    ],
)
def test_malformed_manifest(run, transform, message):
    rewrite(run["dir"] / "manifest.json", transform)

    with pytest.raises(ManifestError, match=message):
        prepare_run(7, RUN_ID, run["root"])


@pytest.mark.parametrize(
    "edit,message",
    [
        (lambda m: m.update(run_id="other-run"), "run ID"),
        (lambda m: m["parameters"].update(case_id=8), "case ID"),
        (lambda m: m.update(manifest_schema_version="2"), "schema version"),
        (lambda m: m["generator"].update(git_commit="not-a-commit"), "git commit"),
        (lambda m: m["report"].update(file="../report.md"), "report file"),
        (lambda m: m["parameters"].update(previous_year=2025), "years"),
        (lambda m: m["evidence_snapshot"]["claims"][0].update(source_id=99),
         "outside the snapshot"),
        (lambda m: m["evidence_snapshot"]["sources"][0].update(
            evidence_assessment="VERIFIED"), "assessment"),
    ],
)
def test_inconsistent_manifest_rejected(run, edit, message):
    def transform(data):
        manifest = json.loads(data)
        edit(manifest)
        return json.dumps(manifest).encode()

    rewrite(run["dir"] / "manifest.json", transform)

    with pytest.raises(ManifestError, match=message):
        prepare_run(7, RUN_ID, run["root"])


def test_run_path_format():
    assert run_path(1, "20261024T233834131742Z-9abc1991") == (
        "case_1/20261024T233834131742Z-9abc1991"
    )


# --- Transaction control flow ---------------------------------


@pytest.fixture
def connection():
    conn = MagicMock()
    conn.in_transaction = False
    return conn


@pytest.fixture
def db(monkeypatch, run):
    """Replace SQL helpers so register_run's control flow is tested."""

    calls = {"inserted": [], "existing": None, "installed": True}

    monkeypatch.setattr(
        registry, "registry_installed", lambda c: calls["installed"]
    )
    monkeypatch.setattr(
        registry, "fetch_registration",
        lambda c, run_id, lock=False: calls["existing"],
    )
    monkeypatch.setattr(registry, "_check_against_evidence", lambda cur, r: None)
    monkeypatch.setattr(
        registry, "_insert", lambda cur, r: calls["inserted"].append(r)
    )
    calls["expected"] = prepare_run(7, RUN_ID, run["root"])
    return calls


def test_successful_registration(db, run, connection):
    result = register_run(connection, 7, RUN_ID, run["root"])

    assert result == {"status": "REGISTERED", "run_id": RUN_ID}
    assert db["inserted"] == [db["expected"]]
    connection.start_transaction.assert_called_once()
    connection.commit.assert_called_once()
    connection.rollback.assert_not_called()


def test_idempotent_repeat_registration(db, run, connection):
    db["existing"] = dict(db["expected"], registered_at_utc="earlier")

    result = register_run(connection, 7, RUN_ID, run["root"])

    assert result["status"] == "ALREADY_REGISTERED"
    assert result["registered_at_utc"] == "earlier"
    assert db["inserted"] == []
    connection.commit.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("report_sha256", "b" * 64),
        ("manifest_sha256", "c" * 64),
        ("evidence_snapshot_sha256", "d" * 64),
        ("git_commit", "e" * 40),
    ],
)
def test_conflicting_duplicate_run_id(db, run, connection, field, value):
    existing = json.loads(json.dumps(db["expected"], default=str))
    existing["run"] = dict(db["expected"]["run"], **{field: value})
    existing["claims"] = db["expected"]["claims"]
    existing["sources"] = db["expected"]["sources"]
    db["existing"] = existing

    with pytest.raises(RegistrationConflict, match=field):
        register_run(connection, 7, RUN_ID, run["root"])

    assert db["inserted"] == []
    connection.commit.assert_not_called()
    connection.rollback.assert_called_once()


def test_conflicting_claim_references(db, run, connection):
    db["existing"] = dict(db["expected"], claims=[(11, "f" * 64, 1)])

    with pytest.raises(RegistrationConflict, match="claim references"):
        register_run(connection, 7, RUN_ID, run["root"])


def test_failure_mid_registration_rolls_back(db, run, connection, monkeypatch):
    def fail(cursor, rows):
        raise errors.DatabaseError("simulated failure after run row")

    monkeypatch.setattr(registry, "_insert", fail)

    with pytest.raises(errors.DatabaseError, match="simulated"):
        register_run(connection, 7, RUN_ID, run["root"])

    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()


def test_concurrent_duplicate_resolved(db, run, connection, monkeypatch):
    def race(cursor, rows):
        db["existing"] = dict(db["expected"], registered_at_utc="concurrent")
        raise errors.IntegrityError(msg="Duplicate entry", errno=1062)

    monkeypatch.setattr(registry, "_insert", race)

    result = register_run(connection, 7, RUN_ID, run["root"])

    assert result["status"] == "ALREADY_REGISTERED"
    assert connection.rollback.call_count == 2
    connection.commit.assert_not_called()


def test_registry_not_installed(db, run, connection):
    db["installed"] = False

    with pytest.raises(RegistryUnavailable):
        register_run(connection, 7, RUN_ID, run["root"])

    # The check runs inside the owned transaction, which is
    # rolled back; nothing is written or committed.
    connection.start_transaction.assert_called_once()
    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()
    assert db["inserted"] == []


class ImplicitTransactionConnection:
    """
    Models mysql-connector with autocommit off: any query
    implicitly opens a transaction, and start_transaction()
    fails while one is open (as the real driver does).
    """

    def __init__(self):
        self.in_transaction = False
        self.log = []

    def start_transaction(self, **kwargs):
        if self.in_transaction:
            raise errors.ProgrammingError("Transaction already in progress")
        self.in_transaction = True
        self.log.append("start")

    def commit(self):
        self.in_transaction = False
        self.log.append("commit")

    def rollback(self):
        self.in_transaction = False
        self.log.append("rollback")

    def cursor(self, **kwargs):
        connection = self

        class Cursor:
            def execute(self, sql, params=()):
                connection.in_transaction = True
                connection.log.append("query")

            def close(self):
                pass

        return Cursor()


@pytest.fixture
def implicit_db(monkeypatch, run):
    """SQL helpers that issue a query, as the real ones do."""

    state = {"existing": None, "inserted": []}

    def query(connection):
        connection.cursor().execute("SELECT 1")

    def installed(connection):
        query(connection)
        return True

    def fetch(connection, run_id, lock=False):
        query(connection)
        return state["existing"]

    monkeypatch.setattr(registry, "registry_installed", installed)
    monkeypatch.setattr(registry, "fetch_registration", fetch)
    monkeypatch.setattr(
        registry, "_check_against_evidence", lambda cursor, r: cursor.execute("x")
    )
    monkeypatch.setattr(
        registry, "_insert",
        lambda cursor, r: (cursor.execute("x"), state["inserted"].append(r)),
    )
    state["expected"] = prepare_run(7, RUN_ID, run["root"])
    return state


def test_clean_connection_preflight_does_not_break_transaction(implicit_db, run):
    """Regression: the installed-check used to open an implicit transaction."""

    connection = ImplicitTransactionConnection()

    result = register_run(connection, 7, RUN_ID, run["root"])

    assert result["status"] == "REGISTERED"
    assert connection.log[0] == "start"          # no query before the transaction
    assert connection.log[-1] == "commit"
    assert connection.log.count("start") == 1
    assert not connection.in_transaction


def test_implicit_transaction_from_earlier_query_refused(implicit_db, run):
    connection = ImplicitTransactionConnection()
    connection.cursor().execute("SELECT caller work")

    with pytest.raises(ValueError, match="possibly implicit"):
        register_run(connection, 7, RUN_ID, run["root"])

    # The caller's transaction is neither committed nor rolled back.
    assert connection.log == ["query"]
    assert connection.in_transaction
    assert implicit_db["inserted"] == []


def test_explicit_caller_transaction_refused_untouched(implicit_db, run):
    connection = ImplicitTransactionConnection()
    connection.start_transaction()

    with pytest.raises(ValueError, match="open transaction"):
        register_run(connection, 7, RUN_ID, run["root"])

    assert connection.log == ["start"]
    assert connection.in_transaction


def test_idempotent_repeat_leaves_connection_clean(implicit_db, run):
    implicit_db["existing"] = dict(
        implicit_db["expected"], registered_at_utc="earlier"
    )
    connection = ImplicitTransactionConnection()

    result = register_run(connection, 7, RUN_ID, run["root"])

    assert result["status"] == "ALREADY_REGISTERED"
    assert "commit" not in connection.log
    assert not connection.in_transaction


def test_failure_leaves_connection_clean_for_reuse(implicit_db, run, monkeypatch):
    def fail(cursor, rows):
        cursor.execute("x")
        raise errors.DatabaseError("simulated failure")

    monkeypatch.setattr(registry, "_insert", fail)
    connection = ImplicitTransactionConnection()

    with pytest.raises(errors.DatabaseError):
        register_run(connection, 7, RUN_ID, run["root"])

    assert connection.log[-1] == "rollback"
    assert "commit" not in connection.log
    assert not connection.in_transaction

    # The same connection can register afterwards.
    monkeypatch.setattr(
        registry, "_insert", lambda cursor, r: cursor.execute("x")
    )
    assert register_run(connection, 7, RUN_ID, run["root"])["status"] == "REGISTERED"


def test_deadlock_retry_starts_each_attempt_clean(implicit_db, run, monkeypatch):
    attempts = []

    def deadlock_once(cursor, rows):
        cursor.execute("x")
        attempts.append(1)
        if len(attempts) == 1:
            raise errors.DatabaseError(msg="Deadlock found", errno=1213)

    monkeypatch.setattr(registry, "_insert", deadlock_once)
    connection = ImplicitTransactionConnection()

    result = register_run(connection, 7, RUN_ID, run["root"])

    assert result["status"] == "REGISTERED"
    assert connection.log.count("start") == 2
    assert connection.log.count("commit") == 1
    assert not connection.in_transaction


def test_open_transaction_refused(db, run, connection):
    connection.in_transaction = True

    with pytest.raises(ValueError, match="open transaction"):
        register_run(connection, 7, RUN_ID, run["root"])


def test_invalid_run_never_reaches_database(run, connection):
    (run["dir"] / "manifest.json").unlink()

    with pytest.raises(RunFilesError):
        register_run(connection, 7, RUN_ID, run["root"])

    connection.cursor.assert_not_called()
    connection.start_transaction.assert_not_called()


def test_deadlock_retried_then_resolved(db, run, connection, monkeypatch):
    attempts = []

    def deadlock_once(cursor, rows):
        attempts.append(1)
        if len(attempts) == 1:
            db["existing"] = dict(db["expected"], registered_at_utc="winner")
            raise errors.DatabaseError(msg="Deadlock found", errno=1213)

    monkeypatch.setattr(registry, "_insert", deadlock_once)

    result = register_run(connection, 7, RUN_ID, run["root"])

    # The retry finds the concurrent winner and changes nothing.
    assert result["status"] == "ALREADY_REGISTERED"
    assert len(attempts) == 1
    assert connection.start_transaction.call_count == 2
    connection.commit.assert_not_called()


def test_persistent_deadlock_raises(db, run, connection, monkeypatch):
    def always(cursor, rows):
        raise errors.DatabaseError(msg="Deadlock found", errno=1213)

    monkeypatch.setattr(registry, "_insert", always)

    with pytest.raises(errors.DatabaseError, match="Deadlock"):
        register_run(connection, 7, RUN_ID, run["root"])

    assert connection.start_transaction.call_count == registry.MAX_ATTEMPTS
    connection.commit.assert_not_called()


def test_decision_uses_locked_rows_not_stale_snapshot(db, run, connection, monkeypatch):
    """
    Regression: under REPEATABLE READ a plain read can return the
    transaction's older snapshot (no row) while the locking read
    sees a concurrently committed registration.
    """

    committed = dict(db["expected"], registered_at_utc="concurrent winner")

    def fetch(c, run_id, lock=False):
        return committed if lock else None

    monkeypatch.setattr(registry, "fetch_registration", fetch)

    result = register_run(connection, 7, RUN_ID, run["root"])

    assert result["status"] == "ALREADY_REGISTERED"
    assert result["registered_at_utc"] == "concurrent winner"
    assert db["inserted"] == []
    connection.commit.assert_not_called()


def test_locked_fetch_uses_locking_reads_throughout():
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.fetchone.return_value = None

    registry.fetch_registration(connection, RUN_ID, lock=True)
    assert cursor.execute.call_args_list[0].args[0].endswith("FOR UPDATE")

    cursor.fetchone.return_value = {
        **{c: 1 for c in registry.RUN_COLUMNS},
        "working_tree_dirty": None, "registered_at_utc": None,
    }
    cursor.fetchall.return_value = []
    cursor.execute.reset_mock()

    registry.fetch_registration(connection, RUN_ID, lock=True)
    sql = [call.args[0] for call in cursor.execute.call_args_list]
    assert sql[0].endswith("FOR UPDATE")
    assert sql[1].endswith("FOR SHARE") and "report_run_claims" in sql[1]
    assert sql[2].endswith("FOR SHARE") and "report_run_sources" in sql[2]

    cursor.execute.reset_mock()
    registry.fetch_registration(connection, RUN_ID)
    assert not any(
        "FOR " in call.args[0] for call in cursor.execute.call_args_list
    )
