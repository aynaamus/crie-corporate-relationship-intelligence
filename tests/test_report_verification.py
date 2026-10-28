
import copy
import datetime
import json
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest

import src.generate_versioned_report as gvr
import src.report_verification as verification
from src.generate_versioned_report import ROOT, generate_versioned_report
from src.report_registry import prepare_run
from src.report_verification import (
    capture_current_snapshot,
    check_currency,
    check_integrity,
    check_reproducibility,
    verify_registered_run,
)


NOW = datetime.datetime(2026, 2, 1, 12, 0, tzinfo=datetime.timezone.utc)


def git_head():
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


HEAD = git_head()
needs_git = pytest.mark.skipif(HEAD is None, reason="git repository unavailable")


@pytest.fixture
def snapshot(snapshot_tools):
    t = snapshot_tools
    return t.snapshot([
        t.claim(11, 1, "49", year=2024),
        t.claim(12, 2, "49", year=2025, location="Note 4"),
    ])


def make_run(snapshot, root, code, run_id="run-v"):
    generate_versioned_report(
        case_id=7, output_root=root, snapshot=snapshot,
        run_id=run_id, now=NOW, code_version=code,
    )
    rows = prepare_run(7, run_id, root)
    return {"run": rows["run"], "claims": rows["claims"], "sources": rows["sources"]}


@pytest.fixture
def stored(snapshot, tmp_path):
    return make_run(
        snapshot, tmp_path, {"git_commit": "a" * 40, "working_tree_dirty": False}
    )


def run_file(tmp_path, name, run_id="run-v"):
    path = tmp_path / "case_7" / run_id / name
    path.chmod(0o644)
    return path


# --- Integrity -------------------------------------------------


def test_intact(stored, tmp_path):
    result = check_integrity(stored, tmp_path)

    assert result["status"] == "INTACT"
    assert result["problems"] == []


def test_deleted_run_directory_is_missing(stored, tmp_path):
    shutil.rmtree(tmp_path / "case_7" / "run-v")

    result = check_integrity(stored, tmp_path)

    assert result["status"] == "MISSING"
    assert "not found" in result["problems"][0]


@pytest.mark.parametrize("name", ["report.md", "manifest.json"])
def test_missing_file(stored, tmp_path, name):
    run_file(tmp_path, name).unlink()

    assert check_integrity(stored, tmp_path)["status"] == "MISSING"


def test_altered_report_bytes(stored, tmp_path):
    path = run_file(tmp_path, "report.md")
    path.write_bytes(path.read_bytes() + b"\n")

    result = check_integrity(stored, tmp_path)

    assert result["status"] == "CORRUPTED"
    assert "report SHA-256 differs from registry" in result["problems"]


def test_altered_manifest_bytes(stored, tmp_path):
    path = run_file(tmp_path, "manifest.json")
    path.write_bytes(path.read_bytes().replace(b'"Note 4"', b'"Note 5"'))

    result = check_integrity(stored, tmp_path)

    assert result["status"] == "CORRUPTED"
    assert "manifest SHA-256 differs from registry" in result["problems"]


def test_malformed_manifest_matching_registry(stored, tmp_path):
    path = run_file(tmp_path, "manifest.json")
    path.write_bytes(b"{not json")
    stored["run"]["manifest_sha256"] = verification.sha256_hex(b"{not json")

    assert check_integrity(stored, tmp_path)["status"] == "INVALID_MANIFEST"


def test_registry_rows_disagree_with_files(stored, tmp_path):
    stored["claims"] = [(11, "f" * 64, 1)]

    result = check_integrity(stored, tmp_path)

    assert result["status"] == "REGISTRY_MISMATCH"
    assert "claim references" in result["problems"][0]


def test_symlinked_run_unreadable(stored, tmp_path):
    (tmp_path / "case_7" / "alias").symlink_to(tmp_path / "case_7" / "run-v")
    stored["run"]["run_id"] = "alias"

    assert check_integrity(stored, tmp_path)["status"] == "UNREADABLE"


# --- Reproducibility -------------------------------------------


def test_not_verified_without_integrity(stored, tmp_path):
    run_file(tmp_path, "report.md").write_bytes(b"tampered")

    integrity = check_integrity(stored, tmp_path)
    result = check_reproducibility(stored, integrity, tmp_path)

    assert result["status"] == "NOT_VERIFIED"


@pytest.mark.parametrize(
    "code",
    [
        {"git_commit": None, "working_tree_dirty": None},
        {"git_commit": "a" * 40, "working_tree_dirty": True},
        {"git_commit": "a" * 40, "working_tree_dirty": None},
    ],
)
def test_unknown_or_uncommitted_code_unavailable(snapshot, tmp_path, code):
    stored = make_run(snapshot, tmp_path, code)
    integrity = check_integrity(stored, tmp_path)

    result = check_reproducibility(stored, integrity, tmp_path)

    assert result["status"] == "CODE_VERSION_UNAVAILABLE"


@needs_git
def test_commit_absent_from_repository(stored, tmp_path):
    integrity = check_integrity(stored, tmp_path)

    result = check_reproducibility(stored, integrity, tmp_path)

    assert result["status"] == "CODE_VERSION_UNAVAILABLE"
    assert "not a trusted ancestor" in result["problems"][0]


@needs_git
def test_same_commit_reproduces_in_process(snapshot, tmp_path, monkeypatch):
    code = {"git_commit": HEAD, "working_tree_dirty": False}
    stored = make_run(snapshot, tmp_path, code)
    monkeypatch.setattr(verification, "get_code_version", lambda root: code)

    result = check_reproducibility(
        stored, check_integrity(stored, tmp_path), tmp_path
    )

    assert result["status"] == "REPRODUCIBLE"
    assert result["checked_with"].startswith("current code")


@needs_git
def test_historical_code_used_when_current_code_differs(
    snapshot, tmp_path, monkeypatch
):
    """Current renderer changes must not make an intact run look bad."""

    stored = make_run(
        snapshot, tmp_path, {"git_commit": HEAD, "working_tree_dirty": False}
    )

    # Simulate later code: current rendering produces other bytes.
    real = gvr.render_report
    monkeypatch.setattr(
        gvr, "render_report", lambda *a: real(*a) + "\nNew footer\n"
    )
    monkeypatch.setattr(
        verification, "get_code_version",
        lambda root: {"git_commit": "b" * 40, "working_tree_dirty": False},
    )

    integrity = check_integrity(stored, tmp_path)
    result = check_reproducibility(
        stored, integrity, tmp_path, allow_historical_code=True
    )

    assert integrity["status"] == "INTACT"
    assert result["status"] == "REPRODUCIBLE"
    assert result["checked_with"] == f"historical code at {HEAD[:12]}"


@needs_git
def test_reproduction_mismatch_is_not_corruption(
    snapshot, tmp_path, monkeypatch
):
    # A run whose saved report was not produced by the code it names.
    real = gvr.render_report
    monkeypatch.setattr(
        gvr, "render_report", lambda *a: real(*a) + "\nUnrecorded edit\n"
    )
    stored = make_run(
        snapshot, tmp_path, {"git_commit": HEAD, "working_tree_dirty": False}
    )
    monkeypatch.setattr(gvr, "render_report", real)
    monkeypatch.setattr(
        verification, "get_code_version",
        lambda root: {"git_commit": "b" * 40, "working_tree_dirty": False},
    )

    integrity = check_integrity(stored, tmp_path)
    result = check_reproducibility(
        stored, integrity, tmp_path, allow_historical_code=True
    )

    assert integrity["status"] == "INTACT"
    assert result["status"] == "REPRODUCTION_MISMATCH"


@needs_git
def test_historical_render_failure_not_verified(stored, tmp_path, monkeypatch):
    monkeypatch.setattr(verification, "_git", lambda *a, **k: b"")
    monkeypatch.setattr(
        verification, "_render_with_commit",
        lambda *a: (_ for _ in ()).throw(RuntimeError("ImportError: render_run")),
    )

    result = check_reproducibility(
        stored, check_integrity(stored, tmp_path), tmp_path,
        allow_historical_code=True,
    )

    assert result["status"] == "NOT_VERIFIED"
    assert "render_run" in result["problems"][0]


# --- Currency --------------------------------------------------


def test_current_when_evidence_unchanged(stored, snapshot):
    result = check_currency(stored, lambda case, years: copy.deepcopy(snapshot))

    assert result["status"] == "CURRENT"
    assert result["current_snapshot_sha256"] == stored["run"]["evidence_snapshot_sha256"]


def test_capture_time_alone_does_not_make_stale(stored, snapshot):
    later = dict(snapshot, captured_at_utc="2030-01-01T00:00:00.000000Z")

    assert check_currency(stored, lambda c, y: later)["status"] == "CURRENT"


def test_new_evidence_makes_report_stale(stored, snapshot, snapshot_tools):
    current = copy.deepcopy(snapshot)
    current["claims"].append(snapshot_tools.claim(13, 2, "51", year=2025))

    result = check_currency(stored, lambda c, y: current, saved_snapshot=snapshot)

    assert result["status"] == "STALE"
    assert result["changes"] == {"claims_added": [13]}


def test_correction_makes_report_stale(stored, snapshot, snapshot_tools):
    current = copy.deepcopy(snapshot)
    current["claims"][1] = snapshot_tools.claim(
        14, 2, "49", year=2025, location="Note 5"
    )
    current["corrections"].append({
        "correction_id": 3, "original_claim_id": 12, "replacement_claim_id": 14,
    })

    result = check_currency(stored, lambda c, y: current, saved_snapshot=snapshot)

    assert result["status"] == "STALE"
    assert result["changes"] == {
        "claims_added": [14], "claims_removed": [12], "corrections_added": [3],
    }


def test_assessment_change_makes_report_stale(stored, snapshot, snapshot_tools):
    current = copy.deepcopy(snapshot)
    current["sources"][0] = snapshot_tools.source(1, "INCONCLUSIVE")

    result = check_currency(stored, lambda c, y: current, saved_snapshot=snapshot)

    assert result["status"] == "STALE"
    assert result["changes"] == {"sources_reassessed": [1]}


def test_unavailable_database_is_unknown_not_current(stored):
    def unavailable(case, years):
        raise ConnectionError("MySQL server has gone away")

    result = check_currency(stored, unavailable)

    assert result["status"] == "UNKNOWN"
    assert result["current_snapshot_sha256"] is None
    assert "ConnectionError" in result["problems"][0]


def test_snapshot_schema_change_is_unknown(stored, snapshot):
    stored["run"]["snapshot_schema_version"] = "0"

    assert check_currency(stored, lambda c, y: snapshot)["status"] == "UNKNOWN"


def test_currency_uses_registry_when_files_deleted(stored, snapshot, tmp_path):
    shutil.rmtree(tmp_path / "case_7")

    result = verify_registered_run(
        stored, load_current=lambda c, y: snapshot, runs_root=tmp_path
    )

    assert result["integrity"]["status"] == "MISSING"
    assert result["currency"]["status"] == "CURRENT"


def test_loader_receives_registered_parameters(stored, snapshot):
    seen = []

    def loader(case, years):
        seen.append((case, years))
        return snapshot

    check_currency(stored, loader)

    assert seen == [(7, (2024, 2025))]


# --- Capturing present evidence --------------------------------


def test_capture_current_snapshot_uses_snapshot_rules(monkeypatch):
    connection = MagicMock()
    monkeypatch.setattr(
        verification, "load_evidence_snapshot",
        lambda **kwargs: {"kwargs": kwargs},
    )

    result = capture_current_snapshot(lambda: connection, 7, (2024, 2025))

    assert result["kwargs"] == {
        "case_id": 7, "years": (2024, 2025), "connection": connection,
    }
    connection.cursor.return_value.execute.assert_called_once_with(
        "SET SESSION time_zone = '+00:00'"
    )
    connection.start_transaction.assert_called_once_with(
        consistent_snapshot=True,
        isolation_level="REPEATABLE READ",
        readonly=True,
    )
    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()
    connection.close.assert_called_once()


def test_capture_closes_connection_on_failure(monkeypatch):
    connection = MagicMock()

    def fail(**kwargs):
        raise ValueError("inconsistent snapshot")

    monkeypatch.setattr(verification, "load_evidence_snapshot", fail)

    with pytest.raises(ValueError):
        capture_current_snapshot(lambda: connection, 7, (2024, 2025))

    connection.rollback.assert_called_once()
    connection.close.assert_called_once()


def test_verify_registered_run_reports_each_check(stored, snapshot, tmp_path):
    result = verify_registered_run(
        stored, load_current=lambda c, y: snapshot,
        runs_root=tmp_path, reproducibility=True,
    )

    assert result["run_id"] == "run-v"
    assert result["integrity"]["status"] == "INTACT"
    assert "manifest" not in result["integrity"]
    assert result["reproducibility"]["status"] in {
        "CODE_VERSION_UNAVAILABLE", "NOT_VERIFIED",
    }
    assert result["currency"]["status"] == "CURRENT"
    json.dumps(result)


# --- Historical-code execution security -------------------------


def _differs(monkeypatch):
    monkeypatch.setattr(
        verification, "get_code_version",
        lambda root: {"git_commit": "b" * 40, "working_tree_dirty": False},
    )


@needs_git
def test_historical_code_disabled_by_default(snapshot, tmp_path, monkeypatch):
    stored = make_run(
        snapshot, tmp_path, {"git_commit": HEAD, "working_tree_dirty": False}
    )
    _differs(monkeypatch)

    def never(*args, **kwargs):
        raise AssertionError("historical code executed without opt-in")

    monkeypatch.setattr(verification, "_render_with_commit", never)

    result = check_reproducibility(stored, check_integrity(stored, tmp_path), tmp_path)

    assert result["status"] == "CODE_VERSION_UNAVAILABLE"
    assert "disabled (opt-in)" in result["problems"][0]


@needs_git
def test_historical_subprocess_gets_no_credentials(
    snapshot, tmp_path, monkeypatch
):
    stored = make_run(
        snapshot, tmp_path, {"git_commit": HEAD, "working_tree_dirty": False}
    )
    _differs(monkeypatch)
    monkeypatch.setenv("MYSQL_PASSWORD", "fictional-secret")
    monkeypatch.setenv("MYSQL_USER", "fictional-user")
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else")

    real_run = verification.subprocess.run
    interpreter_calls = []

    def spy(args, **kwargs):
        if args[0] != "git":
            interpreter_calls.append((args, kwargs))
        return real_run(args, **kwargs)

    monkeypatch.setattr(verification.subprocess, "run", spy)

    result = check_reproducibility(
        stored, check_integrity(stored, tmp_path), tmp_path,
        allow_historical_code=True,
    )

    assert result["status"] == "REPRODUCIBLE"
    [(args, kwargs)] = interpreter_calls
    # Fixed interpreter and isolated mode; never chosen by a manifest.
    assert args[:3] == [verification.sys.executable, "-I", "-c"]
    assert kwargs["env"] == verification.HISTORICAL_ENV
    assert not any("MYSQL" in key or "PYTHON" in key for key in kwargs["env"])
    assert "fictional-secret" not in str(kwargs["env"])


@pytest.mark.parametrize(
    "commit",
    [None, "", "64e7cff", "a" * 40, "A" * 40, "--output=/tmp/x" + "0" * 25,
     "HEAD", "0" * 39 + "g"],
)
def test_untrusted_commit_values_rejected(commit):
    assert verification._trusted_commit(commit) is False


@needs_git
def test_head_is_trusted():
    assert verification._trusted_commit(HEAD) is True


@needs_git
def test_non_ancestor_commit_untrusted_even_when_opted_in(
    snapshot, tmp_path, monkeypatch
):
    stored = make_run(
        snapshot, tmp_path, {"git_commit": HEAD, "working_tree_dirty": False}
    )
    _differs(monkeypatch)

    real_git = verification._git

    def not_ancestor(*args, **kwargs):
        if args[0] == "merge-base":
            raise subprocess.CalledProcessError(1, "git merge-base")
        return real_git(*args, **kwargs)

    monkeypatch.setattr(verification, "_git", not_ancestor)

    def never(*args, **kwargs):
        raise AssertionError("untrusted commit executed")

    monkeypatch.setattr(verification, "_render_with_commit", never)

    result = check_reproducibility(
        stored, check_integrity(stored, tmp_path), tmp_path,
        allow_historical_code=True,
    )

    assert result["status"] == "CODE_VERSION_UNAVAILABLE"
    assert "trusted ancestor" in result["problems"][0]


def test_render_refuses_untrusted_commit_directly(tmp_path):
    with pytest.raises(RuntimeError, match="trusted ancestor"):
        verification._render_with_commit("a" * 40, tmp_path / "m.json", 5)


def test_reproducibility_has_no_repository_parameter():
    import inspect

    for function in (check_reproducibility, verify_registered_run):
        assert "repo_root" not in inspect.signature(function).parameters


# --- Three independent states ----------------------------------


@needs_git
def test_disabled_reproduction_does_not_affect_other_checks(
    snapshot, tmp_path, monkeypatch
):
    stored = make_run(
        snapshot, tmp_path, {"git_commit": HEAD, "working_tree_dirty": False}
    )
    _differs(monkeypatch)

    result = verify_registered_run(
        stored, load_current=lambda c, y: snapshot,
        runs_root=tmp_path, reproducibility=True,
    )

    assert result["integrity"]["status"] == "INTACT"
    assert result["reproducibility"]["status"] == "CODE_VERSION_UNAVAILABLE"
    assert result["currency"]["status"] == "CURRENT"


def test_intact_but_stale_is_not_corrupted(stored, snapshot, snapshot_tools, tmp_path):
    current = copy.deepcopy(snapshot)
    current["claims"].append(snapshot_tools.claim(77, 1, "60", year=2025))

    result = verify_registered_run(
        stored, load_current=lambda c, y: current, runs_root=tmp_path
    )

    assert result["integrity"]["status"] == "INTACT"
    assert result["integrity"]["problems"] == []
    assert result["currency"]["status"] == "STALE"
