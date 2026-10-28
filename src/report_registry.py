
"""
Append-only MySQL registry of versioned report runs.

The registry indexes runs written by
src.generate_versioned_report; the report.md and
manifest.json files remain the record. Registering reads
an existing run (it never regenerates or modifies files),
validates it, and stores its identity, hashes, and claim
and source references in one transaction.

Registering the same run again is idempotent only when
every recorded field agrees; any difference raises
RegistrationConflict and changes nothing.
"""

import datetime
import json
import os
import re
import stat
from pathlib import Path

from mysql.connector import errors

from src.evidence_snapshot import (
    ASSESSMENTS_INSTALLED,
    ASSESSMENTS_NOT_INSTALLED,
    LINEAGE_INSTALLED,
    LINEAGE_NOT_INSTALLED,
    SUPPORTED_SNAPSHOT_VERSIONS,
    canonical_json,
    sha256_hex,
    snapshot_sha256,
)
from src.generate_versioned_report import (
    DEFAULT_RUNS_DIR,
    MANIFEST_FILE,
    MANIFEST_SCHEMA_VERSION,
    REPORT_FILE,
    RUN_ID_PATTERN,
)


REGISTRY_TABLES = ("report_runs", "report_run_claims", "report_run_sources")

ASSESSMENTS = {"NOT_REVIEWED", "SUPPORTS", "CONTRADICTS", "INCONCLUSIVE"}

HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
GENERATED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# Generous upper bound; a run file larger than this is not ours.
MAX_RUN_FILE_BYTES = 64 * 1024 * 1024


class RegistryError(Exception):
    """Base class for registry failures."""


class RunFilesError(RegistryError):
    """A run directory or file is missing, unsafe, or unreadable."""


class ManifestError(RegistryError):
    """A manifest is malformed or inconsistent with its run."""


class RunIntegrityError(RegistryError):
    """Saved files do not match the hashes they declare."""


class RegistrationConflict(RegistryError):
    """A run ID is already registered with different metadata."""


class RegistryUnavailable(RegistryError):
    """The registry tables are not installed."""


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def run_path(case_id, run_id):
    """Registry path of a run, relative to the runs root."""

    if not _positive_int(case_id):
        raise ValueError(f"Invalid case ID: {case_id!r}")

    if not isinstance(run_id, str) or not RUN_ID_PATTERN.match(run_id):
        raise ValueError(f"Invalid run ID: {run_id!r}")

    return f"case_{case_id}/{run_id}"


def resolve_run_dir(case_id, run_id, runs_root=DEFAULT_RUNS_DIR):
    """
    Locate a run inside the runs root. Only validated case
    and run identifiers are accepted, so callers cannot
    reach arbitrary paths; symbolic links are refused.
    """

    root = Path(runs_root).resolve()
    run_dir = root / run_path(case_id, run_id)

    if not run_dir.resolve().is_relative_to(root):
        raise RunFilesError("Run path escapes the runs root")

    for path in (run_dir.parent, run_dir):
        if path.is_symlink():
            raise RunFilesError(f"Refusing symbolic link: {path.name}")

    return run_dir


def _read_run_file(run_dir, name):
    path = run_dir / name

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise RunFilesError(f"Missing {name} in run {run_dir.name}") from None

    if not stat.S_ISREG(info.st_mode):
        raise RunFilesError(f"{name} is not a regular file")

    if info.st_size > MAX_RUN_FILE_BYTES:
        raise RunFilesError(f"{name} exceeds the size limit")

    return path.read_bytes()


def read_run(case_id, run_id, runs_root=DEFAULT_RUNS_DIR):
    """Return (manifest_bytes, report_bytes) of a stored run."""

    run_dir = resolve_run_dir(case_id, run_id, runs_root)

    if not run_dir.is_dir():
        raise RunFilesError(f"Run directory not found: {run_path(case_id, run_id)}")

    return (
        _read_run_file(run_dir, MANIFEST_FILE),
        _read_run_file(run_dir, REPORT_FILE),
    )


def _require(condition, message):
    if not condition:
        raise ManifestError(message)


def validate_manifest(manifest, case_id, run_id):
    """
    Check manifest structure and its consistency with the
    requested run. Hash checks are in verify_run_files().
    """

    _require(isinstance(manifest, dict), "Manifest is not a JSON object")
    _require(
        manifest.get("manifest_schema_version") == MANIFEST_SCHEMA_VERSION,
        "Unsupported manifest schema version",
    )
    _require(manifest.get("run_id") == run_id, "Manifest run ID does not match")
    _require(
        isinstance(manifest.get("generated_at_utc"), str)
        and GENERATED_AT.match(manifest["generated_at_utc"]),
        "Invalid generated_at_utc",
    )

    generator = manifest.get("generator")
    _require(isinstance(generator, dict), "Missing generator")
    _require(
        isinstance(generator.get("entry_point"), str)
        and 0 < len(generator["entry_point"]) <= 128,
        "Invalid generator entry point",
    )
    commit = generator.get("git_commit")
    _require(
        commit is None or (isinstance(commit, str) and HEX40.match(commit)),
        "Invalid git commit",
    )
    _require(
        generator.get("working_tree_dirty") in (True, False, None),
        "Invalid working_tree_dirty",
    )

    parameters = manifest.get("parameters")
    _require(isinstance(parameters, dict), "Missing parameters")
    _require(parameters.get("case_id") == case_id, "Manifest case ID does not match")
    previous, current = parameters.get("previous_year"), parameters.get("current_year")
    _require(
        _positive_int(previous) and _positive_int(current) and previous < current,
        "Invalid reporting years",
    )

    _require(
        (manifest.get("hashing") or {}).get("algorithm") == "sha256",
        "Unsupported hash algorithm",
    )

    for field in ("evidence_snapshot_sha256", "analysis_sha256"):
        _require(
            isinstance(manifest.get(field), str) and HEX64.match(manifest[field]),
            f"Invalid {field}",
        )

    report = manifest.get("report")
    _require(isinstance(report, dict), "Missing report entry")
    _require(report.get("file") == REPORT_FILE, "Unexpected report file name")
    _require(
        isinstance(report.get("sha256"), str) and HEX64.match(report["sha256"]),
        "Invalid report sha256",
    )
    _require(_positive_int(report.get("bytes")), "Invalid report size")

    _require(isinstance(manifest.get("analysis"), dict), "Missing analysis")

    snapshot = manifest.get("evidence_snapshot")
    _require(isinstance(snapshot, dict), "Missing evidence snapshot")
    _require(
        isinstance(snapshot.get("snapshot_schema_version"), str),
        "Missing snapshot schema version",
    )
    _require(
        snapshot["snapshot_schema_version"] in SUPPORTED_SNAPSHOT_VERSIONS,
        "Unsupported snapshot schema version",
    )
    _require(snapshot.get("case_id") == case_id, "Snapshot case ID does not match")
    _require(
        snapshot.get("years") == [previous, current],
        "Snapshot years do not match parameters",
    )

    sources = snapshot.get("sources")
    claims = snapshot.get("claims")
    _require(isinstance(sources, list) and sources, "Snapshot has no sources")
    _require(isinstance(claims, list) and claims, "Snapshot has no claims")
    _require(isinstance(snapshot.get("corrections"), list), "Missing corrections")

    source_ids = set()
    for source in sources:
        _require(isinstance(source, dict), "Invalid source record")
        source_id = source.get("source_id")
        _require(_positive_int(source_id), "Invalid source ID")
        _require(source_id not in source_ids, "Duplicate source ID")
        source_ids.add(source_id)
        _require(
            source.get("evidence_assessment") in ASSESSMENTS,
            "Invalid evidence assessment",
        )
        history_id = source.get("latest_assessment_history_id")
        _require(
            history_id is None or _positive_int(history_id),
            "Invalid assessment history ID",
        )

    claim_ids = set()
    for claim in claims:
        _require(isinstance(claim, dict), "Invalid claim record")
        claim_id = claim.get("claim_id")
        _require(_positive_int(claim_id), "Invalid claim ID")
        _require(claim_id not in claim_ids, "Duplicate claim ID")
        claim_ids.add(claim_id)
        _require(
            isinstance(claim.get("claim_fingerprint"), str)
            and HEX64.match(claim["claim_fingerprint"]),
            "Invalid claim fingerprint",
        )
        _require(
            claim.get("source_id") in source_ids,
            "Claim references a source outside the snapshot",
        )

    if snapshot["snapshot_schema_version"] in ("2", "3", "4"):
        _validate_v2(manifest, snapshot, claim_ids, source_ids)

    if snapshot["snapshot_schema_version"] in ("3", "4"):
        _validate_v3(manifest, snapshot, source_ids)

    if snapshot["snapshot_schema_version"] == "4":
        _validate_v4(snapshot, source_ids)


def _validate_v4(snapshot, source_ids):
    """Version-4 identity and GLEIF context are present and kept apart."""

    from src.claim_subjects import STATUSES

    identity, context = snapshot.get("case_identity"), snapshot.get("gleif_context")
    _require(isinstance(identity, dict) and isinstance(context, dict),
             "Missing version-4 identity or GLEIF context")
    _require(identity.get("schema") in ("INSTALLED", "NOT_INSTALLED"),
             "Invalid identity schema state")
    mappings = identity.get("mappings")
    _require(isinstance(mappings, list), "Missing identity mappings")
    _require(identity["schema"] == "INSTALLED" or not mappings,
             "Mappings recorded without the identity schema")
    ids = set()
    for m in mappings:
        _require(isinstance(m, dict) and isinstance(m.get("mapping_id"), int)
                 and m.get("status") in STATUSES, "Invalid identity mapping")
        _require(m["mapping_id"] not in ids, "Duplicate identity mapping")
        _require(m.get("evidence_source_id") is None or m["evidence_source_id"] in source_ids,
                 "Mapping references a source outside the snapshot")
        ids.add(m["mapping_id"])
    _require(context.get("observation_schema") in ("INSTALLED", "NOT_INSTALLED"),
             "Invalid GLEIF observation schema state")
    _require(isinstance(context.get("entities"), list), "Missing GLEIF entity context")
    for history in context["entities"] + ([context["relationship"]]
                                          if context.get("relationship") else []):
        _require(isinstance(history, dict) and history.get("assessment", {}).get(
            "corporate_change_determination") == "INSUFFICIENT_EVIDENCE",
            "GLEIF context must not determine corporate changes")


def _validate_v3(manifest, snapshot, source_ids):
    """Version-3 source-relationship assessments are well formed."""

    from src.source_assessments import BASES, RELATIONSHIPS

    _require(
        snapshot.get("source_assessments_schema")
        in (ASSESSMENTS_INSTALLED, ASSESSMENTS_NOT_INSTALLED),
        "Invalid source-assessment schema state",
    )
    assessments = snapshot.get("source_assessments")
    _require(isinstance(assessments, list), "Missing source assessments")
    _require(
        snapshot["source_assessments_schema"] == ASSESSMENTS_INSTALLED or not assessments,
        "Assessments recorded without the assessment schema",
    )
    ids = set()
    for a in assessments:
        _require(isinstance(a, dict), "Invalid source assessment")
        _require(_positive_int(a.get("assessment_id")), "Invalid assessment ID")
        _require(a["assessment_id"] not in ids, "Duplicate assessment ID")
        ids.add(a["assessment_id"])
        _require(a.get("source_a_id") in source_ids and a.get("source_b_id") in source_ids,
                 "Assessment references a source outside the snapshot")
        _require(a["source_a_id"] < a["source_b_id"], "Assessment pair is not canonical")
        _require(a.get("relationship") in RELATIONSHIPS, "Invalid assessment relationship")
        _require(a.get("basis") in BASES, "Invalid assessment basis")
        evidence = a.get("evidence_source_id")
        _require(evidence is None or evidence in source_ids,
                 "Assessment evidence source outside the snapshot")
        supersedes = a.get("supersedes_assessment_id")
        _require(supersedes is None or supersedes in ids,
                 "Assessment supersedes an unknown or later assessment")

    for row in manifest["analysis"]["reconciliation"]:
        _require(isinstance(row.get("independence"), str),
                 "Missing independence details")


def _validate_v2(manifest, snapshot, claim_ids, source_ids):
    """Version-2 provenance fields are present and well formed."""

    _require(
        snapshot.get("lineage_schema") in (LINEAGE_INSTALLED, LINEAGE_NOT_INSTALLED),
        "Invalid lineage schema state",
    )
    _require(isinstance(manifest["analysis"].get("lineage"), dict),
             "Missing lineage diagnostics")

    historical = snapshot.get("historical_claims")
    _require(isinstance(historical, list), "Missing historical claims")
    for claim in historical:
        _require(isinstance(claim, dict), "Invalid historical claim record")
        claim_id = claim.get("claim_id")
        _require(_positive_int(claim_id), "Invalid historical claim ID")
        _require(claim_id not in claim_ids, "Claim is both current and historical")
        claim_ids.add(claim_id)
        _require(
            isinstance(claim.get("claim_fingerprint"), str)
            and HEX64.match(claim["claim_fingerprint"]),
            "Invalid historical claim fingerprint",
        )
        _require(
            claim.get("source_id") in source_ids,
            "Historical claim references a source outside the snapshot",
        )

    for correction in snapshot["corrections"]:
        _require(isinstance(correction, dict), "Invalid correction record")
        _require(
            correction.get("correction_kind") in ("CORRECTION", "RESTORATION"),
            "Invalid correction kind",
        )
        restored = correction.get("restored_claim_id")
        _require(restored is None or _positive_int(restored),
                 "Invalid restored claim ID")
        evidence = correction.get("evidence_source_id")
        _require(evidence is None or _positive_int(evidence),
                 "Invalid evidence source ID")
        _require(
            correction.get("attribution_recorded") is (evidence is not None),
            "Inconsistent evidence attribution",
        )


def verify_run_files(manifest, manifest_bytes, report_bytes):
    """
    Return integrity problems between a manifest and its
    files (empty list means consistent). This proves the
    files are unaltered, not that the disclosures are true.
    """

    problems = []

    if sha256_hex(report_bytes) != manifest["report"]["sha256"]:
        problems.append("report SHA-256 does not match manifest")

    if len(report_bytes) != manifest["report"]["bytes"]:
        problems.append("report size does not match manifest")

    if snapshot_sha256(manifest["evidence_snapshot"]) != manifest["evidence_snapshot_sha256"]:
        problems.append("evidence snapshot hash does not match manifest")

    if sha256_hex(canonical_json(manifest["analysis"])) != manifest["analysis_sha256"]:
        problems.append("analysis hash does not match manifest")

    return problems


def build_registration(manifest, manifest_bytes, case_id, run_id):
    """Rows to store for a validated run."""

    generator = manifest["generator"]
    parameters = manifest["parameters"]
    snapshot = manifest["evidence_snapshot"]
    dirty = generator["working_tree_dirty"]

    run = {
        "run_id": run_id,
        "case_id": case_id,
        "previous_year": parameters["previous_year"],
        "current_year": parameters["current_year"],
        "manifest_schema_version": manifest["manifest_schema_version"],
        "snapshot_schema_version": snapshot["snapshot_schema_version"],
        "generated_at_utc": datetime.datetime.strptime(
            manifest["generated_at_utc"], "%Y-%m-%dT%H:%M:%SZ"
        ),
        "entry_point": generator["entry_point"],
        "git_commit": generator["git_commit"],
        "working_tree_dirty": None if dirty is None else int(dirty),
        "evidence_snapshot_sha256": manifest["evidence_snapshot_sha256"],
        "analysis_sha256": manifest["analysis_sha256"],
        "report_sha256": manifest["report"]["sha256"],
        "manifest_sha256": sha256_hex(manifest_bytes),
        "report_bytes": manifest["report"]["bytes"],
        "run_path": run_path(case_id, run_id),
        "claim_count": len(snapshot["claims"]),
        "source_count": len(snapshot["sources"]),
    }

    claims = sorted(
        (c["claim_id"], c["claim_fingerprint"], c["source_id"])
        for c in snapshot["claims"]
    )
    sources = sorted(
        (
            s["source_id"],
            s["evidence_assessment"],
            s["latest_assessment_history_id"],
        )
        for s in snapshot["sources"]
    )

    return {"run": run, "claims": claims, "sources": sources}


def prepare_run(case_id, run_id, runs_root=DEFAULT_RUNS_DIR):
    """
    Read, parse, validate, and integrity-check a stored run.
    Returns its registration rows; raises on any problem.
    """

    manifest_bytes, report_bytes = read_run(case_id, run_id, runs_root)

    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ManifestError(f"Manifest is not valid JSON: {error}") from None

    validate_manifest(manifest, case_id, run_id)

    problems = verify_run_files(manifest, manifest_bytes, report_bytes)

    if problems:
        raise RunIntegrityError("; ".join(problems))

    return build_registration(manifest, manifest_bytes, case_id, run_id)


RUN_COLUMNS = (
    "run_id", "case_id", "previous_year", "current_year",
    "manifest_schema_version", "snapshot_schema_version",
    "generated_at_utc", "entry_point", "git_commit",
    "working_tree_dirty", "evidence_snapshot_sha256",
    "analysis_sha256", "report_sha256", "manifest_sha256",
    "report_bytes", "run_path", "claim_count", "source_count",
)


def registry_installed(connection):
    """True when all registry tables exist in the connected database."""

    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN "
            f"({', '.join(['%s'] * len(REGISTRY_TABLES))})",
            REGISTRY_TABLES,
        )
        return cursor.fetchone()[0] == len(REGISTRY_TABLES)
    finally:
        cursor.close()


def fetch_registration(connection, run_id, lock=False):
    """
    Stored rows for a run in build_registration() form, or None.

    lock=True uses locking reads throughout (FOR UPDATE on the
    run, FOR SHARE on its claim and source rows). Under
    REPEATABLE READ a plain read returns the transaction's
    earlier snapshot, so a registration committed concurrently
    after that snapshot is only visible to locking reads.
    """

    cursor = connection.cursor(dictionary=True)
    share = " FOR SHARE" if lock else ""

    try:
        cursor.execute(
            f"SELECT {', '.join(RUN_COLUMNS)}, registered_at_utc "
            "FROM report_runs WHERE run_id = %s"
            + (" FOR UPDATE" if lock else ""),
            (run_id,),
        )
        row = cursor.fetchone()

        if row is None:
            return None

        cursor.execute(
            "SELECT claim_id, claim_fingerprint, source_id "
            "FROM report_run_claims WHERE run_id = %s ORDER BY claim_id"
            + share,
            (run_id,),
        )
        claims = [
            (int(r["claim_id"]), r["claim_fingerprint"], int(r["source_id"]))
            for r in cursor.fetchall()
        ]

        cursor.execute(
            "SELECT source_id, evidence_assessment, assessment_history_id "
            "FROM report_run_sources WHERE run_id = %s ORDER BY source_id"
            + share,
            (run_id,),
        )
        sources = [
            (
                int(r["source_id"]),
                r["evidence_assessment"],
                None if r["assessment_history_id"] is None
                else int(r["assessment_history_id"]),
            )
            for r in cursor.fetchall()
        ]
    finally:
        cursor.close()

    run = {column: row[column] for column in RUN_COLUMNS}
    for column in ("case_id", "previous_year", "current_year",
                   "report_bytes", "claim_count", "source_count"):
        run[column] = int(run[column])
    if run["working_tree_dirty"] is not None:
        run["working_tree_dirty"] = int(run["working_tree_dirty"])

    return {
        "run": run,
        "claims": claims,
        "sources": sources,
        "registered_at_utc": row["registered_at_utc"],
    }


def _differences(expected, stored):
    fields = [
        column for column in RUN_COLUMNS
        if expected["run"][column] != stored["run"][column]
    ]
    if expected["claims"] != stored["claims"]:
        fields.append("claim references")
    if expected["sources"] != stored["sources"]:
        fields.append("source references")
    return fields


def _check_against_evidence(cursor, registration):
    """The run must reference this case's real claims and sources."""

    run = registration["run"]
    source_ids = [s[0] for s in registration["sources"]]
    slots = ", ".join(["%s"] * len(source_ids))

    cursor.execute(
        f"SELECT source_id FROM case_sources "
        f"WHERE case_id = %s AND source_id IN ({slots})",
        (run["case_id"], *source_ids),
    )
    found = {int(r[0]) for r in cursor.fetchall()}
    if found != set(source_ids):
        raise RegistryError(
            "Sources not registered for this case: "
            f"{sorted(set(source_ids) - found)}"
        )

    claim_ids = [c[0] for c in registration["claims"]]
    slots = ", ".join(["%s"] * len(claim_ids))
    cursor.execute(
        f"SELECT claim_id, claim_fingerprint, source_id "
        f"FROM disclosure_claims WHERE claim_id IN ({slots})",
        claim_ids,
    )
    stored = sorted(
        (int(r[0]), r[1], int(r[2])) for r in cursor.fetchall()
    )
    if stored != registration["claims"]:
        raise RegistryError(
            "Manifest claims do not match registered claims "
            "(missing claim, fingerprint, or source mismatch)"
        )

    history_ids = [s[2] for s in registration["sources"] if s[2] is not None]
    if history_ids:
        slots = ", ".join(["%s"] * len(history_ids))
        cursor.execute(
            f"SELECT history_id, source_id FROM source_assessment_history "
            f"WHERE history_id IN ({slots})",
            history_ids,
        )
        owners = {int(r[0]): int(r[1]) for r in cursor.fetchall()}
        for source_id, _, history_id in registration["sources"]:
            if history_id is not None and owners.get(history_id) != source_id:
                raise RegistryError(
                    f"Assessment history {history_id} does not belong "
                    f"to source {source_id}"
                )


def _insert(cursor, registration):
    run = registration["run"]
    cursor.execute(
        f"INSERT INTO report_runs ({', '.join(RUN_COLUMNS)}) "
        f"VALUES ({', '.join(['%s'] * len(RUN_COLUMNS))})",
        tuple(run[column] for column in RUN_COLUMNS),
    )
    cursor.executemany(
        "INSERT INTO report_run_claims "
        "(run_id, claim_id, claim_fingerprint, source_id) "
        "VALUES (%s, %s, %s, %s)",
        [(run["run_id"], *claim) for claim in registration["claims"]],
    )
    cursor.executemany(
        "INSERT INTO report_run_sources "
        "(run_id, source_id, evidence_assessment, assessment_history_id) "
        "VALUES (%s, %s, %s, %s)",
        [(run["run_id"], *source) for source in registration["sources"]],
    )


def _resolve_existing(registration, stored):
    """Compare a new registration with the stored one."""

    if stored is None:
        raise RegistryError(
            f"Run {registration['run']['run_id']} was reported as already "
            "registered but its registration is not visible; nothing was "
            "changed."
        )

    differences = _differences(registration, stored)

    if differences:
        raise RegistrationConflict(
            f"Run {registration['run']['run_id']} is already registered "
            f"with different {', '.join(differences)}; nothing was changed."
        )

    return {
        "status": "ALREADY_REGISTERED",
        "run_id": registration["run"]["run_id"],
        "registered_at_utc": stored["registered_at_utc"],
    }


# Lock conflicts InnoDB may raise when two sessions register
# the same new run at once; the losing session retries.
RETRYABLE_ERRNOS = {1205, 1213}
MAX_ATTEMPTS = 3


def register_run(connection, case_id, run_id, runs_root=DEFAULT_RUNS_DIR):
    """
    Register an existing run without modifying its files.

    Transaction contract: the caller owns the connection
    (and closes it), but must pass it with no transaction
    open, explicit or implicit. With autocommit off, any
    earlier query, even a SELECT, opens an implicit
    transaction; end it before calling. An open transaction
    is refused with ValueError before any query is issued,
    so caller work is never committed or rolled back here.

    register_run then owns every transaction it starts: all
    of its queries, including the registry-installed check,
    run inside that transaction, which is committed only
    after every row is written and rolled back on any
    failure. The connection is returned with no
    transaction open.

    Returns {"status": "REGISTERED" | "ALREADY_REGISTERED", ...}.
    Raises RegistrationConflict, RunFilesError, ManifestError,
    RunIntegrityError, RegistryUnavailable, or RegistryError.
    """

    # File validation needs no database access.
    registration = prepare_run(case_id, run_id, runs_root)

    if connection.in_transaction:
        raise ValueError(
            "Connection has an open transaction (possibly implicit, "
            "from an earlier query); commit or roll back before "
            "registering. Nothing was changed."
        )

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return _register_once(connection, registration)
        except errors.DatabaseError as error:
            if error.errno not in RETRYABLE_ERRNOS or attempt == MAX_ATTEMPTS:
                raise


def _register_once(connection, registration):
    run_id = registration["run"]["run_id"]

    connection.start_transaction(isolation_level="REPEATABLE READ")
    cursor = connection.cursor()

    try:
        if not registry_installed(connection):
            raise RegistryUnavailable(
                "Report registry tables are not installed"
            )

        existing = fetch_registration(connection, run_id, lock=True)

        if existing is not None:
            # Decide from the locked, current rows; a plain re-read
            # could return this transaction's older snapshot.
            result = _resolve_existing(registration, existing)
            connection.rollback()
            return result

        _check_against_evidence(cursor, registration)
        _insert(cursor, registration)
        connection.commit()

    except errors.IntegrityError as error:
        connection.rollback()

        # A concurrent registration of the same run won the race.
        # After the rollback, a new transaction sees the committed winner.
        if error.errno == 1062:
            try:
                return _resolve_existing(
                    registration, fetch_registration(connection, run_id)
                )
            finally:
                connection.rollback()
        raise

    except BaseException:
        connection.rollback()
        raise

    finally:
        cursor.close()

    return {"status": "REGISTERED", "run_id": run_id}


def list_registered_runs(connection, case_id):
    """Registered runs for a case, newest first."""

    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            f"SELECT {', '.join(RUN_COLUMNS)}, registered_at_utc "
            "FROM report_runs WHERE case_id = %s "
            "ORDER BY generated_at_utc DESC, run_id DESC",
            (case_id,),
        )
        return cursor.fetchall()
    finally:
        cursor.close()


# Preflight outcomes, most severe first.
PREFLIGHT_EVIDENCE_MISMATCH = "EVIDENCE_MISMATCH"
PREFLIGHT_CONFLICT = "CONFLICT"
PREFLIGHT_NOT_INSTALLED = "REGISTRY_NOT_INSTALLED"
PREFLIGHT_ALREADY_REGISTERED = "ALREADY_REGISTERED"
PREFLIGHT_READY = "READY"


def preflight_registration(connection, registration):
    """
    Read-only database checks for a prepared registration
    (from prepare_run), without writing anything.

    Same connection contract as register_run(): no transaction
    may be open. All checks run in one READ ONLY transaction
    that is rolled back, so the server itself rejects writes.

    Evidence compatibility is checked even when the registry
    is not installed. Returns {"status", "problems",
    "registry_installed"} with status one of EVIDENCE_MISMATCH,
    CONFLICT, REGISTRY_NOT_INSTALLED, ALREADY_REGISTERED, READY.
    """

    if connection.in_transaction:
        raise ValueError(
            "Connection has an open transaction (possibly implicit, "
            "from an earlier query); commit or roll back before "
            "preflight. Nothing was changed."
        )

    connection.start_transaction(readonly=True)
    cursor = connection.cursor()

    try:
        installed = registry_installed(connection)

        try:
            _check_against_evidence(cursor, registration)
        except RegistryError as error:
            return {
                "status": PREFLIGHT_EVIDENCE_MISMATCH,
                "problems": [str(error)],
                "registry_installed": installed,
            }

        if not installed:
            return {
                "status": PREFLIGHT_NOT_INSTALLED,
                "problems": ["report registry tables are not installed"],
                "registry_installed": False,
            }

        stored = fetch_registration(connection, registration["run"]["run_id"])

        if stored is None:
            return {"status": PREFLIGHT_READY, "problems": [],
                    "registry_installed": True}

        differences = _differences(registration, stored)

        if differences:
            return {
                "status": PREFLIGHT_CONFLICT,
                "problems": [
                    "already registered with different "
                    + ", ".join(differences)
                ],
                "registry_installed": True,
            }

        return {"status": PREFLIGHT_ALREADY_REGISTERED, "problems": [],
                "registry_installed": True}

    finally:
        cursor.close()
        connection.rollback()
