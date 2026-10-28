
"""
Snapshot-driven, versioned investigation reports.

Entry point:
    python -m src.generate_versioned_report [--case-id 1]
        [--previous-year 2024] [--current-year 2025]
        [--output-root reports/runs]

Each successful run creates a new, never-overwritten
directory reports/runs/case_<id>/<run_id>/ containing
report.md and manifest.json. Run directories are
kept outside Git (reports/runs/ is ignored).
"""

import argparse
import datetime
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if not __package__:
    sys.path.insert(0, str(ROOT))

from src.evidence_snapshot import (  # noqa: E402
    canonical_json,
    load_evidence_snapshot,
    sha256_hex,
    snapshot_sha256,
)
from src.report_renderer import (  # noqa: E402
    analyze_snapshot,
    render_report,
)


MANIFEST_SCHEMA_VERSION = "1"

DEFAULT_RUNS_DIR = ROOT / "reports/runs"
RUNS_PATHSPEC = "reports/runs"

REPORT_FILE = "report.md"
MANIFEST_FILE = "manifest.json"

RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

SERIALIZATION = (
    "canonical JSON: UTF-8, sorted keys, separators (',', ':'), "
    "ensure_ascii=false, NaN rejected"
)


def get_code_version(root=ROOT):
    """
    Git commit and working-tree state, or None where
    undeterminable. Run outputs are excluded from the
    dirty check.
    """

    def git(*args):
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout

    try:
        commit = git("rev-parse", "HEAD").strip()
    except (OSError, subprocess.SubprocessError):
        return {"git_commit": None, "working_tree_dirty": None}

    try:
        status = git(
            "status", "--porcelain", "--",
            ".", f":(exclude){RUNS_PATHSPEC}",
        )
        dirty = bool(status.strip())
    except (OSError, subprocess.SubprocessError):
        dirty = None

    return {"git_commit": commit, "working_tree_dirty": dirty}


def _utc(now):
    now = now or datetime.datetime.now(datetime.timezone.utc)

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    return now.astimezone(datetime.timezone.utc)


def new_run_id(now):
    return (
        f"{now:%Y%m%dT%H%M%S}{now.microsecond:06d}Z-"
        f"{secrets.token_hex(4)}"
    )


def build_manifest(snapshot, analysis, metadata, report_bytes):
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": metadata["run_id"],
        "generated_at_utc": metadata["generated_at_utc"],
        "generator": {
            "entry_point": "src.generate_versioned_report",
            "git_commit": metadata["git_commit"],
            "working_tree_dirty": metadata["working_tree_dirty"],
        },
        "parameters": {
            "case_id": snapshot["case_id"],
            "previous_year": metadata["previous_year"],
            "current_year": metadata["current_year"],
        },
        "hashing": {
            "algorithm": "sha256",
            "serialization": SERIALIZATION,
            "evidence_excludes": ["captured_at_utc"],
        },
        "evidence_snapshot_sha256": metadata["evidence_snapshot_sha256"],
        "evidence_snapshot": snapshot,
        "analysis_sha256": sha256_hex(canonical_json(analysis)),
        "analysis": analysis,
        "report": {
            "file": REPORT_FILE,
            "sha256": sha256_hex(report_bytes),
            "bytes": len(report_bytes),
        },
    }


def _write_new(path, data):
    """Create a read-only file (never replace one), flushed to disk."""

    with open(path, "xb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())

    os.chmod(path, 0o444)


def write_run(case_dir, run_id, report_bytes, manifest_bytes):
    """
    Atomically publish a run directory.

    Files are written into a hidden temporary directory
    and renamed into place only when complete. An existing
    run is never overwritten. On failure the temporary
    directory is removed, leaving no partial run.
    """

    case_dir = Path(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)

    final = case_dir / run_id
    temporary = case_dir / f".tmp-{run_id}"

    if final.exists():
        raise FileExistsError(f"Report run already exists: {final}")

    temporary.mkdir()

    try:
        _write_new(temporary / REPORT_FILE, report_bytes)
        _write_new(temporary / MANIFEST_FILE, manifest_bytes)

        if final.exists():
            raise FileExistsError(
                f"Report run already exists: {final}"
            )

        os.rename(temporary, final)

    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    # Persist the directory entry for the rename.
    descriptor = os.open(case_dir, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

    return final


def _validate_years(previous_year, current_year):
    for year in (previous_year, current_year):
        if not isinstance(year, int) or isinstance(year, bool):
            raise ValueError("Reporting years must be integers")

    if previous_year >= current_year:
        raise ValueError(
            "previous_year must be earlier than current_year"
        )


def render_run(snapshot, previous_year, current_year,
               run_id, generated_at_utc, code_version):
    """Pure: snapshot and run metadata to (report, manifest) bytes."""

    analysis = analyze_snapshot(snapshot, previous_year, current_year)

    metadata = {
        "run_id": run_id,
        "generated_at_utc": generated_at_utc,
        "git_commit": code_version["git_commit"],
        "working_tree_dirty": code_version["working_tree_dirty"],
        "evidence_snapshot_sha256": snapshot_sha256(snapshot),
        "previous_year": previous_year,
        "current_year": current_year,
    }

    report_bytes = render_report(
        snapshot, analysis, metadata
    ).encode("utf-8")

    manifest = build_manifest(snapshot, analysis, metadata, report_bytes)

    manifest_bytes = (
        json.dumps(
            manifest,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n"
    ).encode("utf-8")

    return report_bytes, manifest_bytes, manifest


def generate_versioned_report(
    case_id=1,
    previous_year=2024,
    current_year=2025,
    output_root=DEFAULT_RUNS_DIR,
    connection=None,
    snapshot=None,
    run_id=None,
    now=None,
    code_version=None,
    snapshot_version=None,
):
    """
    Capture (or accept) a snapshot, analyze it, and write a
    new immutable run. Returns the run directory, run ID,
    and hashes. snapshot_version selects the snapshot version read
    (default: the default version); version 4 is never chosen
    implicitly. Earlier runs are never rewritten.

    snapshot, run_id, now, and code_version may be injected
    for deterministic tests. connection follows
    load_evidence_snapshot() ownership rules.
    """

    _validate_years(previous_year, current_year)

    now = _utc(now)
    run_id = run_id or new_run_id(now)

    if not RUN_ID_PATTERN.match(run_id):
        raise ValueError(f"Invalid run ID: {run_id!r}")

    if snapshot is None:
        snapshot = load_evidence_snapshot(
            case_id=case_id,
            years=(previous_year, current_year),
            connection=connection,
            **({"version": snapshot_version} if snapshot_version else {}),
        )

    if snapshot["case_id"] != case_id or sorted(
        snapshot["years"]
    ) != [previous_year, current_year]:
        raise ValueError(
            "Snapshot case or years do not match the requested run"
        )

    report_bytes, manifest_bytes, manifest = render_run(
        snapshot,
        previous_year,
        current_year,
        run_id,
        now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        code_version or get_code_version(),
    )

    run_dir = write_run(
        Path(output_root) / f"case_{case_id}",
        run_id,
        report_bytes,
        manifest_bytes,
    )

    return {
        "run_dir": run_dir,
        "run_id": run_id,
        "evidence_snapshot_sha256": manifest["evidence_snapshot_sha256"],
        "report_sha256": manifest["report"]["sha256"],
    }


def verify_run(run_dir):
    """
    Re-check a stored run. Returns a list of problems;
    an empty list means the report and manifest are intact
    and the report reproduces exactly from the manifest.
    """

    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / MANIFEST_FILE).read_text("utf-8"))
    report_bytes = (run_dir / manifest["report"]["file"]).read_bytes()

    problems = []

    if sha256_hex(report_bytes) != manifest["report"]["sha256"]:
        problems.append("report SHA-256 does not match manifest")

    snapshot = manifest["evidence_snapshot"]

    if snapshot_sha256(snapshot) != manifest["evidence_snapshot_sha256"]:
        problems.append("evidence snapshot hash does not match")

    rebuilt, _, _ = render_run(
        snapshot,
        manifest["parameters"]["previous_year"],
        manifest["parameters"]["current_year"],
        manifest["run_id"],
        manifest["generated_at_utc"],
        {
            "git_commit": manifest["generator"]["git_commit"],
            "working_tree_dirty": manifest["generator"]["working_tree_dirty"],
        },
    )

    if rebuilt != report_bytes:
        problems.append(
            "report does not reproduce from the manifest snapshot"
        )

    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate a versioned, snapshot-driven "
        "investigation report."
    )
    parser.add_argument("--case-id", type=int, default=1)
    parser.add_argument("--previous-year", type=int, default=2024)
    parser.add_argument("--current-year", type=int, default=2025)
    parser.add_argument(
        "--output-root", type=Path, default=DEFAULT_RUNS_DIR
    )
    parser.add_argument("--snapshot-version", choices=("1", "2", "3", "4"),
                        help="snapshot version to read (default: the default version)")
    args = parser.parse_args(argv)

    result = generate_versioned_report(
        case_id=args.case_id,
        previous_year=args.previous_year,
        current_year=args.current_year,
        output_root=args.output_root,
        snapshot_version=args.snapshot_version,
    )

    print("Versioned report generated")
    print("Run ID:", result["run_id"])
    print("Evidence snapshot SHA-256:", result["evidence_snapshot_sha256"])
    print("Report SHA-256:", result["report_sha256"])
    print("Output:", result["run_dir"])


if __name__ == "__main__":
    main()
