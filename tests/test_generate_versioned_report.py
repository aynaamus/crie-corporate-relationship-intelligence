
import datetime
import hashlib
import json
from pathlib import Path

import mysql.connector
import pytest

import src.generate_versioned_report as gvr
from src.evidence_snapshot import snapshot_sha256
from src.generate_versioned_report import (
    generate_versioned_report,
    verify_run,
)


ROOT = Path(__file__).resolve().parents[1]

NOW = datetime.datetime(2026, 2, 1, 12, 0, tzinfo=datetime.timezone.utc)
CODE = {"git_commit": "abc123", "working_tree_dirty": False}

LEGACY_ARTIFACTS = [
    ROOT / "reports/spp_investigation_report.md",
    ROOT / "reports/disclosure_comparison.csv",
]


@pytest.fixture
def snapshot(snapshot_tools):
    t = snapshot_tools
    return t.snapshot([
        t.claim(1, 1, "49", year=2024),
        t.claim(2, 2, "49%", year=2025, location="Note 4"),
    ])


def generate(snapshot, output_root, run_id="run-a", **kwargs):
    return generate_versioned_report(
        case_id=snapshot["case_id"],
        output_root=output_root,
        snapshot=snapshot,
        run_id=run_id,
        now=NOW,
        code_version=CODE,
        **kwargs,
    )


def test_run_layout_and_manifest(snapshot, tmp_path):
    result = generate(snapshot, tmp_path)

    run_dir = tmp_path / "case_7" / "run-a"
    assert result["run_dir"] == run_dir
    assert sorted(p.name for p in run_dir.iterdir()) == [
        "manifest.json", "report.md",
    ]

    report = (run_dir / "report.md").read_bytes()
    manifest = json.loads((run_dir / "manifest.json").read_text("utf-8"))

    assert manifest["manifest_schema_version"] == "1"
    assert manifest["run_id"] == "run-a"
    assert manifest["generated_at_utc"] == "2026-02-01T12:00:00Z"
    assert manifest["parameters"] == {
        "case_id": 7, "previous_year": 2024, "current_year": 2025,
    }
    assert manifest["generator"]["git_commit"] == "abc123"
    assert manifest["hashing"]["algorithm"] == "sha256"
    assert manifest["report"]["sha256"] == hashlib.sha256(report).hexdigest()
    assert manifest["evidence_snapshot_sha256"] == snapshot_sha256(snapshot)
    assert manifest["evidence_snapshot"] == snapshot
    assert [c["claim_id"] for c in manifest["evidence_snapshot"]["claims"]] == [1, 2]
    assert manifest["analysis"]["comparison"][0]["comparison_status"] == "UNCHANGED"

    # No hidden temporary directories left behind.
    assert [p.name for p in (tmp_path / "case_7").iterdir()] == ["run-a"]


def test_manifest_contains_no_secrets(snapshot, tmp_path, monkeypatch):
    monkeypatch.setenv("MYSQL_PASSWORD", "fictional-secret-value")
    generate(snapshot, tmp_path)

    text = (tmp_path / "case_7/run-a/manifest.json").read_text("utf-8")
    assert "fictional-secret-value" not in text
    assert "MYSQL" not in text


def test_reproducible_from_fixed_snapshot(snapshot, tmp_path):
    generate(snapshot, tmp_path / "one")
    generate(snapshot, tmp_path / "two")

    for name in ("report.md", "manifest.json"):
        assert (
            (tmp_path / "one/case_7/run-a" / name).read_bytes()
            == (tmp_path / "two/case_7/run-a" / name).read_bytes()
        )

    assert verify_run(tmp_path / "one/case_7/run-a") == []


def test_verify_detects_tampering(snapshot, tmp_path):
    run_dir = generate(snapshot, tmp_path)["run_dir"]

    report = run_dir / "report.md"
    report.chmod(0o644)
    report.write_text(report.read_text("utf-8").replace("49%", "48%"))

    problems = verify_run(run_dir)
    assert "report SHA-256 does not match manifest" in problems


def test_existing_run_never_overwritten(snapshot, tmp_path):
    generate(snapshot, tmp_path)
    original = (tmp_path / "case_7/run-a/report.md").read_bytes()

    changed = json.loads(json.dumps(snapshot))
    changed["claims"][1]["source_location"] = "Note 9"

    with pytest.raises(FileExistsError):
        generate(changed, tmp_path)

    assert (tmp_path / "case_7/run-a/report.md").read_bytes() == original


def test_new_run_ids_are_unique(snapshot, tmp_path):
    first = generate(snapshot, tmp_path, run_id=None)
    second = generate(snapshot, tmp_path, run_id=None)

    assert first["run_id"] != second["run_id"]
    assert first["run_id"].startswith("20260201T120000000000Z-")
    assert len(list((tmp_path / "case_7").iterdir())) == 2


@pytest.mark.parametrize("bad", ["../escape", "a/b", ".hidden", "", "x" * 200])
def test_invalid_run_ids_rejected(snapshot, tmp_path, bad):
    with pytest.raises(ValueError):
        generate(snapshot, tmp_path, run_id=bad or "!")


def test_rename_failure_leaves_no_artifacts(snapshot, tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(gvr.os, "rename", fail)

    with pytest.raises(OSError, match="simulated"):
        generate(snapshot, tmp_path)

    assert list((tmp_path / "case_7").iterdir()) == []


def test_partial_write_failure_leaves_no_artifacts(
    snapshot, tmp_path, monkeypatch
):
    real = gvr._write_new
    calls = []

    def fail_second(path, data):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("simulated write failure")
        real(path, data)

    monkeypatch.setattr(gvr, "_write_new", fail_second)

    with pytest.raises(OSError, match="simulated write"):
        generate(snapshot, tmp_path)

    assert list((tmp_path / "case_7").iterdir()) == []


def test_render_failure_writes_nothing(snapshot, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("simulated render failure")

    monkeypatch.setattr(gvr, "render_report", fail)

    with pytest.raises(RuntimeError):
        generate(snapshot, tmp_path)

    assert not (tmp_path / "case_7").exists()


def test_snapshot_parameters_must_match(snapshot, tmp_path):
    with pytest.raises(ValueError, match="do not match"):
        generate_versioned_report(
            case_id=99, output_root=tmp_path, snapshot=snapshot,
            run_id="r", now=NOW, code_version=CODE,
        )

    with pytest.raises(ValueError, match="earlier"):
        generate(snapshot, tmp_path, previous_year=2025, current_year=2024)


def test_naive_timestamp_rejected(snapshot, tmp_path):
    with pytest.raises(ValueError, match="timezone-aware"):
        generate_versioned_report(
            case_id=7, output_root=tmp_path, snapshot=snapshot,
            run_id="r", now=datetime.datetime(2026, 1, 1),
            code_version=CODE,
        )


@pytest.mark.skipif(not all(p.is_file() for p in LEGACY_ARTIFACTS),
                    reason="legacy report artifacts are not in this checkout")
def test_legacy_artifacts_preserved(snapshot, tmp_path):
    before = {p: p.read_bytes() for p in LEGACY_ARTIFACTS}

    generate(snapshot, tmp_path)

    assert {p: p.read_bytes() for p in LEGACY_ARTIFACTS} == before
    assert gvr.DEFAULT_RUNS_DIR == ROOT / "reports/runs"


def test_injected_snapshot_needs_no_database(snapshot, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Database connection attempted")

    monkeypatch.setattr(mysql.connector, "connect", fail)

    generate(snapshot, tmp_path)


def test_code_version_reports_commit():
    version = gvr.get_code_version()

    assert version["git_commit"] is None or len(version["git_commit"]) == 40
    assert version["working_tree_dirty"] in (True, False, None)


def test_code_version_without_git(tmp_path, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(gvr.subprocess, "run", missing)

    assert gvr.get_code_version(tmp_path) == {
        "git_commit": None, "working_tree_dirty": None,
    }


def test_published_files_are_read_only(snapshot, tmp_path):
    run_dir = generate(snapshot, tmp_path)["run_dir"]

    for name in ("report.md", "manifest.json"):
        assert (run_dir / name).stat().st_mode & 0o222 == 0
