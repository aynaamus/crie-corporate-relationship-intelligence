
"""
Three independent checks for registered report runs.

- Integrity: are the saved files exactly what was registered
  and internally consistent? (Not proof that disclosures are
  factually correct.)
- Reproducibility: does the saved snapshot, rendered by the
  code version that produced the run, give the same bytes?
- Currency: does the run's evidence snapshot still equal the
  present evidence? STALE means the report no longer reflects
  the exact current evidence, not that its findings are false.
"""

import datetime
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from src.evidence_snapshot import (
    SUPPORTED_SNAPSHOT_VERSIONS,
    as_version,
    load_evidence_snapshot,
    sha256_hex,
    snapshot_sha256,
)
from src.generate_versioned_report import (
    DEFAULT_RUNS_DIR,
    MANIFEST_FILE,
    ROOT,
    get_code_version,
    render_run,
)
from src.report_registry import (
    ManifestError,
    RunFilesError,
    _differences,
    build_registration,
    read_run,
    resolve_run_dir,
    validate_manifest,
    verify_run_files,
)


# Integrity statuses
INTACT = "INTACT"
MISSING = "MISSING"
UNREADABLE = "UNREADABLE"
INVALID_MANIFEST = "INVALID_MANIFEST"
CORRUPTED = "CORRUPTED"
REGISTRY_MISMATCH = "REGISTRY_MISMATCH"

# Reproducibility statuses
REPRODUCIBLE = "REPRODUCIBLE"
REPRODUCTION_MISMATCH = "REPRODUCTION_MISMATCH"
CODE_VERSION_UNAVAILABLE = "CODE_VERSION_UNAVAILABLE"
NOT_VERIFIED = "NOT_VERIFIED"

# Currency statuses
CURRENT = "CURRENT"
STALE = "STALE"
UNKNOWN = "UNKNOWN"


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _result(status, problems=(), **extra):
    return {"status": status, "problems": list(problems), **extra}


def check_integrity(stored, runs_root=DEFAULT_RUNS_DIR):
    """
    Compare a registration (from fetch_registration) with
    the files on disk. Returns {"status", "problems",
    "manifest": dict | None}.
    """

    run = stored["run"]
    case_id, run_id = run["case_id"], run["run_id"]

    try:
        manifest_bytes, report_bytes = read_run(case_id, run_id, runs_root)
    except RunFilesError as error:
        message = str(error)
        status = (
            MISSING
            if message.startswith(("Missing", "Run directory not found"))
            else UNREADABLE
        )
        return _result(status, [message], manifest=None)

    problems = []

    if sha256_hex(manifest_bytes) != run["manifest_sha256"]:
        problems.append("manifest SHA-256 differs from registry")

    if sha256_hex(report_bytes) != run["report_sha256"]:
        problems.append("report SHA-256 differs from registry")

    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        validate_manifest(manifest, case_id, run_id)
    except (UnicodeDecodeError, ValueError, ManifestError) as error:
        return _result(
            INVALID_MANIFEST if not problems else CORRUPTED,
            problems + [f"manifest invalid: {error}"],
            manifest=None,
        )

    problems += verify_run_files(manifest, manifest_bytes, report_bytes)

    if problems:
        return _result(CORRUPTED, problems, manifest=manifest)

    # Files match their registered hashes; any remaining
    # difference means the registry rows disagree with them.
    differences = _differences(
        build_registration(manifest, manifest_bytes, case_id, run_id),
        stored,
    )

    if differences:
        return _result(
            REGISTRY_MISMATCH,
            [f"registry differs in {', '.join(differences)}"],
            manifest=manifest,
        )

    return _result(INTACT, manifest=manifest)


HISTORICAL_RENDER = """
import hashlib, json, sys
sys.path.insert(0, sys.argv[1])
from src.generate_versioned_report import render_run
m = json.loads(open(sys.argv[2], encoding="utf-8").read())
g = m["generator"]
report, _, _ = render_run(
    m["evidence_snapshot"],
    m["parameters"]["previous_year"],
    m["parameters"]["current_year"],
    m["run_id"],
    m["generated_at_utc"],
    {"git_commit": g["git_commit"],
     "working_tree_dirty": g["working_tree_dirty"]},
)
print(hashlib.sha256(report).hexdigest())
"""


COMMIT = re.compile(r"^[0-9a-f]{40}$")

# The only environment passed to historical code: no credentials,
# no database settings, no PYTHON* variables (-I also ignores those).
HISTORICAL_ENV = {
    "PATH": os.defpath,
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}


def _git(*args, **kwargs):
    """git in this project's own repository only."""

    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        check=True,
        timeout=60,
        **kwargs,
    ).stdout


def _trusted_commit(commit):
    """
    Historical code may only come from this repository's own
    history: a full commit hash that is an ancestor of HEAD.
    Unreachable, fetched-but-unmerged, or abbreviated commits
    are not trusted.
    """

    if not isinstance(commit, str) or not COMMIT.match(commit):
        return False

    try:
        _git("cat-file", "-e", f"{commit}^{{commit}}")
        _git("merge-base", "--is-ancestor", commit, "HEAD")
    except (OSError, subprocess.SubprocessError):
        return False

    return True


def _render_with_commit(commit, manifest_path, timeout):
    """
    Render a manifest with the src/ tree of a trusted past
    commit in a separate interpreter. Returns the report
    SHA-256.

    This is process separation, not a security sandbox: the
    code runs with the user's operating-system permissions.
    It is therefore limited to trusted commits, receives a
    minimal environment without credentials, runs in an empty
    temporary directory, and is time-limited.
    """

    if not _trusted_commit(commit):
        raise RuntimeError("commit is not a trusted ancestor of HEAD")

    archive = _git("archive", "--format=tar", commit, "--", "src")

    with tempfile.TemporaryDirectory(prefix="crie-render-") as workdir:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(workdir, filter="data")

        completed = subprocess.run(
            [sys.executable, "-I", "-c", HISTORICAL_RENDER,
             workdir, str(manifest_path)],
            cwd=workdir,
            env=HISTORICAL_ENV,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip().splitlines()[-1]
            if completed.stderr.strip() else "historical render failed"
        )

    return completed.stdout.strip()


def check_reproducibility(stored, integrity, runs_root=DEFAULT_RUNS_DIR,
                          allow_historical_code=False, timeout=120):
    """
    Re-render the saved snapshot with the code version that
    produced the run. Only meaningful for intact runs.

    When the running code is the run's commit on a clean tree,
    it renders in-process (this code is already executing).
    Otherwise reproduction needs the run's historical code,
    which executes only when allow_historical_code=True and
    only from a trusted ancestor of HEAD. By default the
    result is CODE_VERSION_UNAVAILABLE instead.
    """

    if integrity["status"] != INTACT:
        return _result(
            NOT_VERIFIED, ["integrity check did not pass"], checked_with=None
        )

    manifest = integrity["manifest"]
    generator = manifest["generator"]
    commit = generator["git_commit"]

    if (
        not isinstance(commit, str)
        or not COMMIT.match(commit)
        or generator["working_tree_dirty"] is not False
    ):
        return _result(
            CODE_VERSION_UNAVAILABLE,
            ["run was generated from unknown or uncommitted code"],
            checked_with=None,
        )

    current = get_code_version(ROOT)
    same_code = current == {"git_commit": commit, "working_tree_dirty": False}

    if not same_code:
        if not _trusted_commit(commit):
            return _result(
                CODE_VERSION_UNAVAILABLE,
                [f"commit {commit[:12]} is not a trusted ancestor of "
                 "HEAD in this repository"],
                checked_with=None,
            )

        if not allow_historical_code:
            return _result(
                CODE_VERSION_UNAVAILABLE,
                [f"reproduction needs historical code at {commit[:12]}; "
                 "historical code execution is disabled (opt-in)"],
                checked_with=None,
            )

    try:
        if same_code:
            report, _, _ = render_run(
                manifest["evidence_snapshot"],
                manifest["parameters"]["previous_year"],
                manifest["parameters"]["current_year"],
                manifest["run_id"],
                manifest["generated_at_utc"],
                {"git_commit": commit, "working_tree_dirty": False},
            )
            rendered = sha256_hex(report)
            checked_with = "current code (same commit, clean tree)"
        else:
            run_dir = resolve_run_dir(
                stored["run"]["case_id"], stored["run"]["run_id"], runs_root
            )
            rendered = _render_with_commit(
                commit, run_dir / MANIFEST_FILE, timeout
            )
            checked_with = f"historical code at {commit[:12]}"
    except Exception as error:
        return _result(
            NOT_VERIFIED, [f"reproduction could not run: {error}"],
            checked_with=None,
        )

    if rendered == manifest["report"]["sha256"]:
        return _result(REPRODUCIBLE, checked_with=checked_with)

    return _result(
        REPRODUCTION_MISMATCH,
        ["re-rendered report differs from the saved report"],
        checked_with=checked_with,
    )


def capture_current_snapshot(connect, case_id, years, version=None):
    """
    Read present evidence with the established snapshot
    rules: own connection from connect(), UTC session,
    read-only consistent REPEATABLE READ transaction. version
    defaults to the default snapshot version; a version-4 run
    needs version="4" to be comparable.
    """

    connection = connect()

    try:
        cursor = connection.cursor()
        try:
            cursor.execute("SET SESSION time_zone = '+00:00'")
        finally:
            cursor.close()

        connection.start_transaction(
            consistent_snapshot=True,
            isolation_level="REPEATABLE READ",
            readonly=True,
        )

        try:
            return load_evidence_snapshot(
                case_id=case_id, years=years, connection=connection,
                **({"version": version} if version else {}),
            )
        finally:
            connection.rollback()
    finally:
        connection.close()


def diff_snapshots(saved, current):
    """Summarize what changed between two evidence snapshots."""

    def claims(snapshot):
        return {c["claim_id"]: c for c in snapshot["claims"]}

    def sources(snapshot):
        return {
            s["source_id"]: (
                s["evidence_assessment"], s["latest_assessment_history_id"]
            )
            for s in snapshot["sources"]
        }

    saved_claims, current_claims = claims(saved), claims(current)
    saved_sources, current_sources = sources(saved), sources(current)

    changes = {
        "claims_added": sorted(set(current_claims) - set(saved_claims)),
        "claims_removed": sorted(set(saved_claims) - set(current_claims)),
        "claims_changed": sorted(
            i for i in set(saved_claims) & set(current_claims)
            if saved_claims[i] != current_claims[i]
        ),
        "corrections_added": sorted(
            {c["correction_id"] for c in current["corrections"]}
            - {c["correction_id"] for c in saved["corrections"]}
        ),
        "sources_added": sorted(set(current_sources) - set(saved_sources)),
        "sources_reassessed": sorted(
            i for i in set(saved_sources) & set(current_sources)
            if saved_sources[i] != current_sources[i]
        ),
    }

    return {key: value for key, value in changes.items() if value}


def check_currency(stored, load_current, saved_snapshot=None):
    """
    Compare the registered evidence hash with the present
    evidence. load_current(case_id, years) returns a fresh
    snapshot; any failure yields UNKNOWN, never CURRENT.

    Present evidence is compared in the run's registered snapshot
    version. STALE means only that the evidence differs; it does
    not mean the report is wrong or damaged. A version-2 run can
    become STALE through provenance alone (recorded evidence, or
    the lineage schema being installed) with unchanged findings.
    """

    run = stored["run"]
    years = (run["previous_year"], run["current_year"])
    checked_at = _now()

    version = run["snapshot_schema_version"]

    if version not in SUPPORTED_SNAPSHOT_VERSIONS:
        return _result(
            UNKNOWN,
            [f"snapshot schema version {version!r} is not supported; "
             "hashes are not comparable"],
            checked_at_utc=checked_at, current_snapshot_sha256=None,
        )

    try:
        # Present evidence, expressed in the run's own snapshot
        # version, so the hashes are comparable.
        current = as_version(load_current(run["case_id"], years), version)
        current_hash = snapshot_sha256(current)
    except Exception as error:
        return _result(
            UNKNOWN,
            [f"current evidence unavailable: {type(error).__name__}"],
            checked_at_utc=checked_at, current_snapshot_sha256=None,
        )

    if current_hash == run["evidence_snapshot_sha256"]:
        return _result(
            CURRENT, checked_at_utc=checked_at,
            current_snapshot_sha256=current_hash,
        )

    changes = diff_snapshots(saved_snapshot, current) if saved_snapshot else {}

    return _result(
        STALE,
        ["evidence changed since the report was generated"],
        checked_at_utc=checked_at,
        current_snapshot_sha256=current_hash,
        changes=changes,
    )


def verify_registered_run(stored, load_current=None,
                          runs_root=DEFAULT_RUNS_DIR,
                          reproducibility=False,
                          allow_historical_code=False):
    """
    Run the requested checks for one registration.
    Historical code runs only with allow_historical_code=True.
    """

    integrity = check_integrity(stored, runs_root)

    result = {
        "run_id": stored["run"]["run_id"],
        "verified_at_utc": _now(),
        "integrity": {k: v for k, v in integrity.items() if k != "manifest"},
    }

    if reproducibility:
        result["reproducibility"] = check_reproducibility(
            stored, integrity, runs_root,
            allow_historical_code=allow_historical_code,
        )

    if load_current is not None:
        manifest = integrity.get("manifest")
        result["currency"] = check_currency(
            stored,
            load_current,
            saved_snapshot=manifest["evidence_snapshot"] if manifest else None,
        )

    return result
