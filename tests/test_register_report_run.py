
import datetime
import hashlib
import io
import json
from pathlib import Path
from unittest.mock import MagicMock

import mysql.connector
import pytest

import src.register_report_run as cli
from src.generate_versioned_report import generate_versioned_report


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.datetime(2026, 2, 1, 12, 0, tzinfo=datetime.timezone.utc)
RUN_ID = "run-cli"
SECRET = "fictional-password-value"

SPP_RUN = ROOT / "reports/runs/case_1/20261024T233834131742Z-9abc1991"
SPP_HASHES = {
    "report.md": "625fa8e945791eced6a2a9ed1cfd40aad559177eb5a5615c6fbd8eb637f6964b",
    "manifest.json": "b2f5cbb725dffd1bedb23f4fc220d2d84fd11ffa559e1a81c301976f87ae42a0",
}


@pytest.fixture
def runs_root(snapshot_tools, tmp_path):
    t = snapshot_tools
    generate_versioned_report(
        case_id=7, output_root=tmp_path,
        snapshot=t.snapshot([
            t.claim(11, 1, "49", year=2024),
            t.claim(12, 2, "49", year=2025),
        ]),
        run_id=RUN_ID, now=NOW,
        code_version={"git_commit": None, "working_tree_dirty": None},
    )
    return tmp_path


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("MYSQL_USER", "fictional-user")
    monkeypatch.setenv("MYSQL_PASSWORD", SECRET)
    # A misleading default that must never be used as the target.
    monkeypatch.setenv("MYSQL_DATABASE", "corporate_intelligence")
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)


def fake_connection(database):
    connection = MagicMock()
    connection.in_transaction = False
    connection.cursor.return_value.fetchone.return_value = (database,)
    return connection


@pytest.fixture
def connector(monkeypatch, env):
    """Record every connection request; answer with a fake server."""

    calls = []

    def connect(**kwargs):
        calls.append(kwargs)
        return fake_connection(kwargs["database"])

    monkeypatch.setattr(cli.mysql.connector, "connect", connect)
    return calls


@pytest.fixture
def preflight(monkeypatch):
    state = {"status": "READY", "installed": True, "calls": 0}

    def fake(connection, registration):
        state["calls"] += 1
        return {
            "status": state["status"],
            "problems": [] if state["status"] in ("READY", "ALREADY_REGISTERED")
            else ["details"],
            "registry_installed": state["installed"],
        }

    monkeypatch.setattr(cli, "preflight_registration", fake)
    return state


def invoke(*args):
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(list(args), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def base(runs_root, database="corporate_intelligence_test", *extra):
    return ("--case-id", "7", "--run-id", RUN_ID, "--database", database,
            "--runs-root", str(runs_root), *extra)


def run_hashes(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


# --- Target selection and production protection ---------------


def test_dry_run_is_default_and_ready(runs_root, connector, preflight):
    before = run_hashes(runs_root)

    code, out, err = invoke(*base(runs_root))

    assert code == 0
    assert "DRY RUN" in out
    assert "READY TO REGISTER (dry run; nothing was written)" in out
    assert [c["database"] for c in connector] == ["corporate_intelligence_test"]
    assert run_hashes(runs_root) == before


def test_target_is_exactly_the_requested_database(runs_root, connector, preflight):
    invoke(*base(runs_root))

    # MYSQL_DATABASE points at production; it must be ignored.
    assert connector[0]["database"] == "corporate_intelligence_test"


def test_unexpected_database_rejected(runs_root, connector):
    with pytest.raises(SystemExit) as exit_info:
        invoke(*base(runs_root, "mysql"))

    assert exit_info.value.code == cli.EXIT_USAGE
    assert connector == []

    with pytest.raises(cli.DatabaseTargetError):
        cli.connect_to("information_schema")


def test_production_commit_requires_confirmation(runs_root, connector, preflight):
    for extra in ((), ("--confirm-production", "yes"),
                  ("--confirm-production", cli.production_phrase("other-run"))):
        code, _, err = invoke(*base(runs_root, "corporate_intelligence",
                                    "--commit", *extra))

        assert code == cli.EXIT_CONFIRMATION
        assert "append-only" in err

    # Refused before any file or database access.
    assert connector == []
    assert preflight["calls"] == 0


def test_confirmation_without_commit_rejected(runs_root, connector):
    code, _, err = invoke(*base(
        runs_root, "corporate_intelligence",
        "--confirm-production", cli.production_phrase(RUN_ID),
    ))

    assert code == cli.EXIT_CONFIRMATION
    assert connector == []


def test_production_dry_run_is_read_only_path(runs_root, connector, preflight,
                                              monkeypatch):
    def never(*a, **k):
        raise AssertionError("register_run called in dry run")

    monkeypatch.setattr(cli, "register_run", never)
    preflight["status"] = "REGISTRY_NOT_INSTALLED"
    preflight["installed"] = False

    code, out, err = invoke(*base(runs_root, "corporate_intelligence"))

    assert code == cli.EXIT_NOT_INSTALLED
    assert "Registry schema: NOT INSTALLED" in err
    assert "Evidence compatibility: CONFIRMED" in err
    assert "NOT READY" in err
    assert len(connector) == 1


def test_confirmed_production_commit_reaches_register_run(
    runs_root, connector, preflight, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        cli, "register_run",
        lambda conn, case, run_id, root: calls.append(run_id)
        or {"status": "REGISTERED", "run_id": run_id},
    )

    code, out, _ = invoke(*base(
        runs_root, "corporate_intelligence", "--commit",
        "--confirm-production", cli.production_phrase(RUN_ID),
    ))

    assert code == 0
    assert calls == [RUN_ID]
    # Preflight and registration each use a fresh connection.
    assert [c["database"] for c in connector] == ["corporate_intelligence"] * 2


# --- Connection failures and identity --------------------------


def test_connection_failure_has_no_fallback(runs_root, monkeypatch, env, preflight):
    calls = []

    def refuse(**kwargs):
        calls.append(kwargs["database"])
        raise mysql.connector.errors.ProgrammingError(
            msg=f"Access denied for user 'fictional-user' (password: {SECRET})",
            errno=1045,
        )

    monkeypatch.setattr(cli.mysql.connector, "connect", refuse)

    code, out, err = invoke(*base(runs_root))

    assert code == cli.EXIT_DATABASE
    assert calls == ["corporate_intelligence_test"]       # tried once, no fallback
    assert "errno 1045" in err
    assert "NOT VERIFIED against the database" in err
    assert preflight["calls"] == 0
    assert SECRET not in out + err
    assert "fictional-user" not in out + err


def test_identity_mismatch_refused(runs_root, monkeypatch, env, preflight):
    connection = fake_connection("corporate_intelligence")
    monkeypatch.setattr(cli.mysql.connector, "connect", lambda **k: connection)

    code, _, err = invoke(*base(runs_root))

    assert code == cli.EXIT_DATABASE
    assert "expected 'corporate_intelligence_test'" in err
    assert preflight["calls"] == 0
    connection.close.assert_called_once()


def test_identity_query_transaction_ended(env, monkeypatch):
    connection = fake_connection("corporate_intelligence_test")
    monkeypatch.setattr(cli.mysql.connector, "connect", lambda **k: connection)

    returned = cli.connect_to("corporate_intelligence_test")

    assert returned is connection
    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()


def test_credentials_never_printed(runs_root, connector, preflight):
    outputs = []
    for status in ("READY", "CONFLICT", "EVIDENCE_MISMATCH",
                   "REGISTRY_NOT_INSTALLED", "ALREADY_REGISTERED"):
        preflight["status"] = status
        outputs.append(invoke(*base(runs_root)))

    text = "".join(out + err for _, out, err in outputs)
    assert SECRET not in text
    assert "fictional-user" not in text


# --- File validation (before any database access) --------------


@pytest.mark.parametrize(
    "run_id", ["../../etc", "a/b", ".hidden", "x" * 200],
)
def test_unsafe_run_ids(runs_root, connector, run_id):
    code, _, err = invoke("--case-id", "7", "--run-id", run_id,
                          "--database", "corporate_intelligence_test",
                          "--runs-root", str(runs_root))

    assert code == cli.EXIT_FILES
    assert connector == []


def test_missing_or_symlinked_runs_root(runs_root, connector, tmp_path_factory):
    missing = tmp_path_factory.mktemp("m") / "absent"
    link = tmp_path_factory.mktemp("l") / "link"
    link.symlink_to(runs_root)

    for root in (missing, link):
        code, _, err = invoke(*base(root))
        assert code == cli.EXIT_FILES

    assert connector == []


@pytest.mark.parametrize("name", ["report.md", "manifest.json"])
def test_missing_run_file(runs_root, connector, name):
    (runs_root / "case_7" / RUN_ID / name).unlink()

    code, _, err = invoke(*base(runs_root))

    assert code == cli.EXIT_FILES
    assert f"Missing {name}" in err
    assert connector == []


@pytest.mark.parametrize(
    "content", [b"{broken", b"[]", json.dumps({"run_id": RUN_ID}).encode()],
)
def test_malformed_manifest(runs_root, connector, content):
    path = runs_root / "case_7" / RUN_ID / "manifest.json"
    path.chmod(0o644)
    path.write_bytes(content)

    code, _, err = invoke(*base(runs_root))

    assert code == cli.EXIT_FILES
    assert connector == []


def test_altered_report_rejected(runs_root, connector):
    path = runs_root / "case_7" / RUN_ID / "report.md"
    path.chmod(0o644)
    path.write_bytes(path.read_bytes() + b" ")

    code, _, err = invoke(*base(runs_root))

    assert code == cli.EXIT_FILES
    assert "report SHA-256" in err


# --- Preflight outcomes ---------------------------------------


@pytest.mark.parametrize(
    "status,code,text",
    [
        ("READY", 0, "READY TO REGISTER"),
        ("ALREADY_REGISTERED", 0, "ALREADY REGISTERED"),
        ("REGISTRY_NOT_INSTALLED", 3, "registry is not installed"),
        ("CONFLICT", 4, "CONFLICT"),
        ("EVIDENCE_MISMATCH", 5, "Evidence compatibility: FAILED"),
    ],
)
def test_preflight_outcomes(runs_root, connector, preflight, status, code, text):
    preflight["status"] = status
    preflight["installed"] = status != "REGISTRY_NOT_INSTALLED"

    result, out, err = invoke(*base(runs_root))

    assert result == code
    assert text in out + err


def test_already_registered_commit_does_not_register(
    runs_root, connector, preflight, monkeypatch
):
    def never(*a, **k):
        raise AssertionError("register_run called for an identical run")

    monkeypatch.setattr(cli, "register_run", never)
    preflight["status"] = "ALREADY_REGISTERED"

    code, out, _ = invoke(*base(runs_root, "corporate_intelligence_test", "--commit"))

    assert code == 0
    assert "ALREADY REGISTERED" in out


def test_commit_errors_mapped(runs_root, connector, preflight, monkeypatch):
    from src.report_registry import RegistrationConflict, RegistryError

    for error, expected in (
        (RegistrationConflict("race lost with different hashes"), cli.EXIT_CONFLICT),
        (RegistryError("evidence changed"), cli.EXIT_EVIDENCE),
        (mysql.connector.errors.DatabaseError(msg="gone", errno=2013),
         cli.EXIT_DATABASE),
    ):
        def fail(*a, error=error, **k):
            raise error

        monkeypatch.setattr(cli, "register_run", fail)
        code, _, err = invoke(*base(runs_root, "corporate_intelligence_test",
                                    "--commit"))
        assert code == expected


@pytest.mark.skipif(not SPP_RUN.is_dir(), reason="local SPP run not present")
def test_historical_spp_files_unchanged():
    for name, expected in SPP_HASHES.items():
        assert hashlib.sha256((SPP_RUN / name).read_bytes()).hexdigest() == expected
