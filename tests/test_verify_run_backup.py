
import datetime
import shutil

import pytest

from src.generate_versioned_report import generate_versioned_report
from src.verify_run_backup import main, verify_runs

NOW = datetime.datetime(2026, 2, 1, 12, 0, tzinfo=datetime.timezone.utc)
CODE = {"git_commit": "0123456789abcdef0123456789abcdef01234567",
        "working_tree_dirty": False}


@pytest.fixture
def runs(tmp_path, snapshot_tools):
    t = snapshot_tools
    snapshot = t.snapshot([t.claim(1, 1, "49", year=2024),
                           t.claim(2, 2, "49%", year=2025, location="Note 4")])
    root = tmp_path / "runs"
    for run_id in ("run-a", "run-b"):
        generate_versioned_report(case_id=snapshot["case_id"], output_root=root,
                                  snapshot=snapshot, run_id=run_id, now=NOW,
                                  code_version=CODE)
    return root


def test_valid_runs_verify(runs, capsys):
    assert verify_runs(runs) == (2, [])
    assert main([str(runs)]) == 0
    assert "2 run(s) verified" in capsys.readouterr().out


def test_tampered_report_detected(runs):
    report = runs / "case_7" / "run-a" / "report.md"
    report.chmod(0o644)
    report.write_text(report.read_text() + "edited\n")

    _, problems = verify_runs(runs)

    assert any("report SHA-256 does not match manifest" in p for p in problems)


@pytest.mark.parametrize("damage", ["stray-file", "unfinished", "symlink", "extra-file"])
def test_unexpected_entries_detected(runs, damage):
    if damage == "stray-file":
        (runs / "notes.txt").write_text("x")
    elif damage == "unfinished":
        (runs / "case_7" / ".tmp-run-c").mkdir()
    elif damage == "symlink":
        (runs / "case_7" / "run-a" / "link").symlink_to("/etc/hosts")
    else:
        (runs / "case_7" / "run-b" / "extra.md").write_text("x")

    _, problems = verify_runs(runs)

    assert problems


def test_backup_copy_must_cover_source(runs, tmp_path, capsys):
    copy = tmp_path / "copy"
    shutil.copytree(runs, copy)
    assert verify_runs(copy, covers=runs) == (2, [])

    shutil.rmtree(copy / "case_7" / "run-b")
    _, problems = verify_runs(copy, covers=runs)

    assert problems == ["case_7/run-b/manifest.json is missing or differs in the backup copy",
                        "case_7/run-b/report.md is missing or differs in the backup copy"]
    assert main([str(copy), "--covers", str(runs)]) == 1
    assert "FAILED" in capsys.readouterr().err


def test_finder_metadata_is_ignored(runs):
    for directory in (runs, runs / "case_7", runs / "case_7" / "run-a"):
        (directory / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")

    assert verify_runs(runs) == (2, [])


@pytest.mark.parametrize("damage", [
    "ds_store_symlink", "ds_store_directory", "other_hidden_file",
    "appledouble_file", "ds_store_variant",
])
def test_other_unexpected_entries_still_rejected(runs, damage):
    run = runs / "case_7" / "run-a"
    if damage == "ds_store_symlink":
        (run / ".DS_Store").symlink_to("/etc/hosts")
    elif damage == "ds_store_directory":
        (run / ".DS_Store").mkdir()
    elif damage == "other_hidden_file":
        (run / ".hidden").write_text("x")
    elif damage == "appledouble_file":
        (run / "._report.md").write_text("x")
    else:
        (run / ".DS_Store.bak").write_text("x")

    _, problems = verify_runs(runs)

    assert problems == ["case_7/run-a does not hold exactly ['manifest.json', 'report.md']"]


def test_finder_metadata_does_not_hide_tampering(runs):
    run = runs / "case_7" / "run-a"
    (run / ".DS_Store").write_bytes(b"x")
    report = run / "report.md"
    report.chmod(0o644)
    report.write_text(report.read_text() + "edited\n")

    _, problems = verify_runs(runs)

    assert any("report SHA-256 does not match manifest" in p for p in problems)
