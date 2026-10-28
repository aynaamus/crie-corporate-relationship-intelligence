
"""
Consistent, read-only evidence snapshots for reporting.

A snapshot captures, in one REPEATABLE READ transaction,
every record a report needs: current claims, source
registry entries, assessment history (with annotations),
source metadata change identifiers, and correction
lineage. Rendering then runs on the snapshot alone.

Snapshots contain only stored evidence. Missing metadata
(e.g. an unrecorded publication date) stays null; nothing
is inferred.

Hashing: snapshot_sha256() is SHA-256 over canonical_json()
of the snapshot with "captured_at_utc" removed, so the same
evidence always produces the same hash.

Versions: "4" (only when requested explicitly) adds two sections that
are kept apart from the case's evidence: "case_identity", the
investigation's claim-subject identity mappings with their full
history (nothing resolved automatically), and "gleif_context", the
read-only GLEIF history (src/relationship_history.py) of the case's
relationship and of every LEI the case names. GLEIF context never
takes part in reconciliation or corroboration. "3" (the default) adds the investigation's recorded
source-relationship assessments, their full history, and whether the
database records them at all ("source_assessments",
"source_assessments_schema"). "2" adds correction provenance to "1":
each correction's kind, restored claim and supporting evidence,
the superseded claims its lineage refers to ("historical_claims"),
and whether the database records lineage at all
("lineage_schema"). A version-1 snapshot is exactly the version-2
snapshot without those additions (as_version), so evidence read
today hashes identically to a version-1 snapshot of the same
evidence taken before version 2 existed.
"""

import datetime
import hashlib
import json

from src.load_current_disclosures import (
    _connect,
    prepare_claims_frame,
)


SNAPSHOT_SCHEMA_VERSION = "3"
SUPPORTED_SNAPSHOT_VERSIONS = ("1", "2", "3", "4")
# Version 4 exists only on request: the default stays "3".
V4_SNAPSHOT_KEYS = ("case_identity", "gleif_context")
INSTALLED = "INSTALLED"
NOT_INSTALLED = "NOT_INSTALLED"
GLEIF_CONTEXT_NOTE = (
    "Global GLEIF source information, the same for every investigation and kept apart "
    "from this case's claims and sources. It is not independent corroboration of any "
    "claim and never changes a claim's verification status. Differences between "
    "published GLEIF records do not establish that, or when, a corporate change "
    "occurred, and accounting consolidation is not evidence of ownership or control.")
IDENTITY_NOTE = (
    "Identity mappings recorded by investigators for this case. Nothing is resolved "
    "automatically; AMBIGUOUS, NO_LEI and UNRESOLVED mappings stay as recorded, and "
    "subjects without a mapping are unmapped.")

ASSESSMENTS_TABLE = "source_relationship_assessments"
ASSESSMENTS_INSTALLED = "INSTALLED"
ASSESSMENTS_NOT_INSTALLED = "NOT_INSTALLED"
V3_SNAPSHOT_KEYS = ("source_assessments", "source_assessments_schema")

# Correction columns added by Milestone 22.4A.
LINEAGE_COLUMNS = (
    "correction_kind", "restored_claim_id",
    "evidence_source_id", "evidence_location",
)
LINEAGE_INSTALLED = "INSTALLED"
LINEAGE_NOT_INSTALLED = "NOT_INSTALLED"

# Version-2 additions, removed again by as_version(..., "1").
V2_SNAPSHOT_KEYS = ("historical_claims", "lineage_schema")
V2_CORRECTION_KEYS = (
    "correction_kind", "restored_claim_id", "evidence_source_id",
    "evidence_location", "attribution_recorded",
)

# Excluded from the evidence hash: it describes when the
# snapshot was read, not what the evidence is.
VOLATILE_FIELDS = ("captured_at_utc",)


def canonical_json(value):
    """
    Deterministic JSON bytes: UTF-8, sorted keys, compact
    separators, no ASCII escaping, NaN rejected.
    """

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def snapshot_sha256(snapshot):
    """Hash the evidence content of a snapshot."""

    evidence = {
        key: value
        for key, value in snapshot.items()
        if key not in VOLATILE_FIELDS
    }

    return sha256_hex(canonical_json(evidence))


def _iso(value):
    """Serialize MySQL temporal values; the session is UTC."""

    if value is None:
        return None

    if isinstance(value, datetime.datetime):
        return value.isoformat(timespec="microseconds") + "Z"

    if isinstance(value, datetime.date):
        return value.isoformat()

    return str(value)


def _text(value):
    return None if value is None else str(value)


def _fetch(cursor, sql, params=()):
    cursor.execute(sql, params)
    return cursor.fetchall()


def _read(connection, case_id, years, v4=False):
    cursor = connection.cursor(dictionary=True)
    year_slots = ", ".join(["%s"] * len(years))

    try:
        case = _fetch(
            cursor,
            "SELECT case_id FROM investigation_cases "
            "WHERE case_id = %s",
            (case_id,),
        )

        if not case:
            raise ValueError(
                f"Investigation case {case_id} not found."
            )

        captured = _fetch(
            cursor, "SELECT UTC_TIMESTAMP(6) AS captured_at_utc"
        )

        sources = _fetch(
            cursor,
            """
            SELECT
                source_id, document_title, publisher,
                source_url, publication_date,
                evidence_assessment, added_at
            FROM case_sources
            WHERE case_id = %s
            ORDER BY source_id
            """,
            (case_id,),
        )

        history = _fetch(
            cursor,
            """
            SELECT
                h.history_id, h.source_id, h.event_type,
                h.previous_assessment, h.new_assessment,
                h.review_reason, h.recorded_at_utc
            FROM source_assessment_history h
            JOIN case_sources s ON s.source_id = h.source_id
            WHERE s.case_id = %s
            ORDER BY h.history_id
            """,
            (case_id,),
        )

        annotations = _fetch(
            cursor,
            """
            SELECT
                a.annotation_id, a.history_id,
                a.annotation_type, a.annotation_note,
                a.created_at_utc
            FROM source_history_annotations a
            JOIN source_assessment_history h
                ON h.history_id = a.history_id
            JOIN case_sources s ON s.source_id = h.source_id
            WHERE s.case_id = %s
            ORDER BY a.annotation_id
            """,
            (case_id,),
        )

        metadata_changes = _fetch(
            cursor,
            """
            SELECT m.change_id, m.source_id, m.changed_at_utc
            FROM source_metadata_history m
            JOIN case_sources s ON s.source_id = m.source_id
            WHERE s.case_id = %s
            ORDER BY m.change_id
            """,
            (case_id,),
        )

        claims = _fetch(
            cursor,
            f"""
            SELECT
                c.claim_id, c.claim_fingerprint,
                c.source_id, c.entity_name, c.metric,
                c.report_year, c.period_end,
                c.value_text, c.normalized_value,
                c.source_location, c.created_at
            FROM v_current_disclosure_claims c
            JOIN case_sources s ON s.source_id = c.source_id
            WHERE s.case_id = %s
              AND c.report_year IN ({year_slots})
            ORDER BY c.claim_id
            """,
            (case_id, *years),
        )

        schema = _fetch(
            cursor,
            "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME IN ('claim_corrections', %s)",
            (ASSESSMENTS_TABLE,),
        )
        present = {
            row["COLUMN_NAME"] for row in schema
            if row["TABLE_NAME"] == "claim_corrections"
        }
        assessments_installed = any(
            row["TABLE_NAME"] == ASSESSMENTS_TABLE for row in schema
        )
        installed = present & set(LINEAGE_COLUMNS)

        if installed and installed != set(LINEAGE_COLUMNS):
            raise ValueError(
                "claim_corrections has only part of the correction-"
                "lineage schema; refusing to guess its meaning."
            )

        # Without the lineage schema every correction predates
        # restorations and evidence attribution.
        lineage = (
            "cc.correction_kind, cc.restored_claim_id, "
            "cc.evidence_source_id, cc.evidence_location"
            if installed else
            "'CORRECTION' AS correction_kind, "
            "NULL AS restored_claim_id, "
            "NULL AS evidence_source_id, NULL AS evidence_location"
        )

        corrections = _fetch(
            cursor,
            f"""
            SELECT
                {lineage},
                cc.correction_id,
                cc.original_claim_id,
                cc.replacement_claim_id,
                cc.correction_reason,
                cc.corrected_at_utc,
                o.source_id AS original_source_id,
                o.metric AS original_metric,
                o.report_year AS original_report_year,
                o.period_end AS original_period_end,
                o.value_text AS original_value,
                o.source_location AS original_location,
                o.claim_fingerprint AS original_fingerprint,
                r.value_text AS replacement_value,
                r.source_location AS replacement_location,
                r.claim_fingerprint AS replacement_fingerprint
            FROM claim_corrections cc
            JOIN disclosure_claims o
                ON o.claim_id = cc.original_claim_id
            JOIN case_sources s ON s.source_id = o.source_id
            -- A replacement outside this case is never read: its
            -- fields stay null and the lineage diagnostics report it.
            LEFT JOIN disclosure_claims r
                ON r.claim_id = cc.replacement_claim_id
               AND r.source_id IN (
                   SELECT source_id FROM case_sources WHERE case_id = %s)
            WHERE s.case_id = %s
              AND o.report_year IN ({year_slots})
            ORDER BY cc.correction_id
            """,
            (case_id, case_id, *years),
        )

        # Superseded claims the lineage refers to, from this case only.
        current_ids = {int(c["claim_id"]) for c in claims}
        referenced = sorted({
            int(row[key])
            for row in corrections
            for key in ("original_claim_id", "replacement_claim_id",
                        "restored_claim_id")
            if row[key] is not None
        } - current_ids)

        historical_claims = []
        if referenced:
            slots = ", ".join(["%s"] * len(referenced))
            historical_claims = _fetch(
                cursor,
                f"""
                SELECT
                    c.claim_id, c.claim_fingerprint,
                    c.source_id, c.entity_name, c.metric,
                    c.report_year, c.period_end,
                    c.value_text, c.normalized_value,
                    c.source_location, c.created_at
                FROM disclosure_claims c
                JOIN case_sources s ON s.source_id = c.source_id
                WHERE s.case_id = %s
                  AND c.claim_id IN ({slots})
                ORDER BY c.claim_id
                """,
                (case_id, *referenced),
            )

        # This investigation's assessments, in the same transaction as
        # the claims and sources they concern.
        source_assessments = []
        if assessments_installed:
            source_assessments = _fetch(
                cursor,
                f"""
                SELECT
                    assessment_id, source_a_id, source_b_id,
                    entity_name, metric, report_year, period_end,
                    relationship, basis, evidence_source_id,
                    evidence_reference, evidence_location, rationale,
                    limitations, recorded_by, recorded_at_utc,
                    supersedes_assessment_id
                FROM {ASSESSMENTS_TABLE}
                WHERE case_id = %s
                ORDER BY assessment_id
                """,
                (case_id,),
            )

    finally:
        cursor.close()

    # Version 4: read in the same consistent transaction.
    v4_rows = _read_v4(connection, case_id) if v4 else None

    return {
        "v4": v4_rows,
        "captured": captured,
        "sources": sources,
        "history": history,
        "annotations": annotations,
        "metadata_changes": metadata_changes,
        "claims": claims,
        "corrections": corrections,
        "historical_claims": historical_claims,
        "lineage_schema": (
            LINEAGE_INSTALLED if installed else LINEAGE_NOT_INSTALLED
        ),
        "source_assessments": source_assessments,
        "source_assessments_schema": (
            ASSESSMENTS_INSTALLED if assessments_installed
            else ASSESSMENTS_NOT_INSTALLED
        ),
    }


class _SnapshotConnection:
    """
    The snapshot's connection for read-only helpers that end their own
    transaction (src/relationship_history.py): their rollback() is ignored
    here, so everything is read in the one consistent snapshot.
    """

    def __init__(self, connection):
        self._connection = connection

    def cursor(self, **kwargs):
        return self._connection.cursor(**kwargs)

    def rollback(self):
        pass


def _read_v4(connection, case_id):
    from src.claim_subjects import fetch_case_parties, fetch_mappings
    from src.relationship_history import entity_history, relationship_history

    mappings = fetch_mappings(connection, case_id)
    parties = fetch_case_parties(connection, case_id)
    cursor = connection.cursor(dictionary=True)
    try:
        relationship = None
        if parties:
            relationship = _fetch(
                cursor, "SELECT child_lei, parent_lei, relationship_type FROM "
                        "investigation_cases WHERE case_id = %s", (case_id,))[0]
        observed = bool(_fetch(
            cursor, "SELECT COUNT(*) AS n FROM information_schema.TABLES WHERE "
                    "TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'relationship_observations'")[0]["n"])
    finally:
        cursor.close()

    leis = set(parties)
    for mapping in mappings or ():
        if mapping["lei"]:
            leis.add(mapping["lei"])
        if mapping["candidate_leis"]:
            candidates = mapping["candidate_leis"]
            leis |= set(json.loads(candidates) if isinstance(candidates, str) else candidates)

    reader = _SnapshotConnection(connection)
    return {
        "mappings": mappings,
        "parties": parties,
        "observation_schema": observed,
        "relationship": None if relationship is None else relationship_history(
            reader, relationship["child_lei"], relationship["parent_lei"],
            relationship["relationship_type"]),
        "entities": [entity_history(reader, lei) for lei in sorted(leis)],
    }


def _mapping_record(row):
    candidates = row["candidate_leis"]
    if isinstance(candidates, str):
        candidates = json.loads(candidates)
    return {
        "mapping_id": int(row["mapping_id"]),
        "subject_text": row["subject_text"],
        "status": row["status"],
        "lei": row["lei"],
        "name_type": row["name_type"],
        "candidate_leis": candidates,
        "basis": row["basis"],
        "evidence_source_id": (int(row["evidence_source_id"])
                               if row["evidence_source_id"] is not None else None),
        "evidence_reference": _text(row["evidence_reference"]),
        "evidence_location": _text(row["evidence_location"]),
        "rationale": row["rationale"],
        "limitations": _text(row["limitations"]),
        "recorded_by": row["recorded_by"],
        "recorded_at_utc": _iso(row["recorded_at_utc"]),
        "supersedes_mapping_id": (int(row["supersedes_mapping_id"])
                                  if row["supersedes_mapping_id"] is not None else None),
    }


def _v4_sections(rows):
    v4 = rows["v4"]
    return {
        "case_identity": {
            "schema": INSTALLED if v4["mappings"] is not None else NOT_INSTALLED,
            "case_parties": list(v4["parties"]),
            "mappings": [_mapping_record(row) for row in v4["mappings"] or ()],
            "note": IDENTITY_NOTE,
        },
        "gleif_context": {
            "observation_schema": INSTALLED if v4["observation_schema"] else NOT_INSTALLED,
            "relationship": v4["relationship"],
            "entities": v4["entities"],
            "note": GLEIF_CONTEXT_NOTE,
        },
    }


def _assessment_record(row):
    scoped = row["metric"] is not None
    return {
        "assessment_id": int(row["assessment_id"]),
        "source_a_id": int(row["source_a_id"]),
        "source_b_id": int(row["source_b_id"]),
        "entity_name": _text(row["entity_name"]) if scoped else None,
        "metric": _text(row["metric"]) if scoped else None,
        "report_year": int(row["report_year"]) if scoped else None,
        "period_end": _iso(row["period_end"]) if scoped else None,
        "relationship": _text(row["relationship"]),
        "basis": _text(row["basis"]),
        "evidence_source_id": (None if row["evidence_source_id"] is None
                               else int(row["evidence_source_id"])),
        "evidence_reference": _text(row["evidence_reference"]),
        "evidence_location": _text(row["evidence_location"]),
        "rationale": _text(row["rationale"]),
        "limitations": _text(row["limitations"]),
        "recorded_by": _text(row["recorded_by"]),
        "recorded_at_utc": _iso(row["recorded_at_utc"]),
        "supersedes_assessment_id": (None if row["supersedes_assessment_id"] is None
                                     else int(row["supersedes_assessment_id"])),
    }


def _claim_record(row):
    return {
        "claim_id": int(row["claim_id"]),
        "claim_fingerprint": _text(row["claim_fingerprint"]),
        "source_id": int(row["source_id"]),
        "entity": _text(row["entity_name"]),
        "metric": _text(row["metric"]),
        "report_year": int(row["report_year"]),
        "period_end": _iso(row["period_end"]),
        "value": _text(row["value_text"]),
        "normalized_value": _text(row["normalized_value"]),
        "source_location": _text(row["source_location"]),
        "created_at_utc": _iso(row["created_at"]),
    }


def _assemble(case_id, years, rows):
    annotations_by_history = {}

    for row in rows["annotations"]:
        annotations_by_history.setdefault(
            int(row["history_id"]), []
        ).append({
            "annotation_id": int(row["annotation_id"]),
            "annotation_type": _text(row["annotation_type"]),
            "annotation_note": _text(row["annotation_note"]),
            "created_at_utc": _iso(row["created_at_utc"]),
        })

    history_by_source = {}

    for row in rows["history"]:
        history_id = int(row["history_id"])
        history_by_source.setdefault(
            int(row["source_id"]), []
        ).append({
            "history_id": history_id,
            "event_type": _text(row["event_type"]),
            "previous_assessment": _text(row["previous_assessment"]),
            "new_assessment": _text(row["new_assessment"]),
            "review_reason": _text(row["review_reason"]),
            "recorded_at_utc": _iso(row["recorded_at_utc"]),
            "annotations": annotations_by_history.get(history_id, []),
        })

    changes_by_source = {}

    for row in rows["metadata_changes"]:
        changes_by_source.setdefault(
            int(row["source_id"]), []
        ).append({
            "change_id": int(row["change_id"]),
            "changed_at_utc": _iso(row["changed_at_utc"]),
        })

    sources = []

    for row in rows["sources"]:
        source_id = int(row["source_id"])
        history = history_by_source.get(source_id, [])
        assessment = _text(row["evidence_assessment"])

        # Cite the latest review that is not an annotated
        # accidental duplicate; both must explain the current
        # assessment, otherwise the snapshot is unreliable.
        effective = [
            h for h in history
            if not any(
                a["annotation_type"] == "ACCIDENTAL_DUPLICATE"
                for a in h["annotations"]
            )
        ]

        latest = (effective or history)[-1] if history else None

        checked = [history[-1], latest] if history else []

        for record in checked:
            if record["new_assessment"] != assessment:
                raise ValueError(
                    f"Source {source_id}: current assessment "
                    f"{assessment} does not match history "
                    f"record {record['history_id']} "
                    f"({record['new_assessment']})."
                )

        sources.append({
            "source_id": source_id,
            "document_title": _text(row["document_title"]),
            "publisher": _text(row["publisher"]),
            "source_url": _text(row["source_url"]),
            "publication_date": _iso(row["publication_date"]),
            "added_at_utc": _iso(row["added_at"]),
            "evidence_assessment": assessment,
            "latest_assessment_history_id": (
                latest["history_id"] if latest else None
            ),
            "assessment_history": history,
            "metadata_changes": changes_by_source.get(source_id, []),
        })

    source_ids = {s["source_id"] for s in sources}

    claims = []

    for row in rows["claims"]:
        if int(row["source_id"]) not in source_ids:
            raise ValueError(
                f"Claim {row['claim_id']} references source "
                f"{row['source_id']} outside the snapshot."
            )

        claims.append(_claim_record(row))

    if not claims:
        raise ValueError(
            f"No current disclosure claims found for "
            f"investigation case {case_id}."
        )

    corrections = [
        {
            "correction_id": int(row["correction_id"]),
            "original_claim_id": int(row["original_claim_id"]),
            "replacement_claim_id": int(row["replacement_claim_id"]),
            "correction_reason": _text(row["correction_reason"]),
            "corrected_at_utc": _iso(row["corrected_at_utc"]),
            "source_id": int(row["original_source_id"]),
            "metric": _text(row["original_metric"]),
            "report_year": int(row["original_report_year"]),
            "period_end": _iso(row["original_period_end"]),
            "original_value": _text(row["original_value"]),
            "original_location": _text(row["original_location"]),
            "original_fingerprint": _text(row["original_fingerprint"]),
            "replacement_value": _text(row["replacement_value"]),
            "replacement_location": _text(row["replacement_location"]),
            "replacement_fingerprint": _text(
                row["replacement_fingerprint"]
            ),
            # Version 2 provenance; None means not recorded.
            "correction_kind": _text(row["correction_kind"]),
            "restored_claim_id": (
                None if row["restored_claim_id"] is None
                else int(row["restored_claim_id"])
            ),
            "evidence_source_id": (
                None if row["evidence_source_id"] is None
                else int(row["evidence_source_id"])
            ),
            "evidence_location": _text(row["evidence_location"]),
            "attribution_recorded": row["evidence_source_id"] is not None,
        }
        for row in rows["corrections"]
    ]

    snapshot = {
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "case_id": int(case_id),
        "years": list(years),
        "captured_at_utc": _iso(
            rows["captured"][0]["captured_at_utc"]
        ),
        "claims": claims,
        "sources": sources,
        "corrections": corrections,
        # Superseded versions only; current claims are in "claims".
        "historical_claims": [
            _claim_record(row) for row in rows["historical_claims"]
        ],
        "lineage_schema": rows["lineage_schema"],
        "source_assessments": [
            _assessment_record(row) for row in rows["source_assessments"]
        ],
        "source_assessments_schema": rows["source_assessments_schema"],
    }

    for assessment in snapshot["source_assessments"]:
        for key in ("source_a_id", "source_b_id", "evidence_source_id"):
            if assessment[key] is not None and assessment[key] not in source_ids:
                raise ValueError(
                    f"Assessment {assessment['assessment_id']} references "
                    f"source {assessment[key]} outside the snapshot."
                )

    if rows.get("v4") is not None:
        snapshot["snapshot_schema_version"] = "4"
        snapshot.update(_v4_sections(rows))
        for mapping in snapshot["case_identity"]["mappings"]:
            if mapping["evidence_source_id"] is not None and \
                    mapping["evidence_source_id"] not in source_ids:
                raise ValueError(f"Mapping {mapping['mapping_id']} references source "
                                 f"{mapping['evidence_source_id']} outside the snapshot.")

    # Validate claims with the loader's rules (non-strict:
    # competing claims are kept for reconciliation).
    snapshot_claims_frame(snapshot)

    return snapshot


def as_version(snapshot, version):
    """
    The same evidence in snapshot version `version`. Each version is
    the next one without its additions (4 -> 3 -> 2 -> 1); a snapshot cannot
    be raised to a later version, because that evidence was never read.
    """

    if version not in SUPPORTED_SNAPSHOT_VERSIONS:
        raise ValueError(f"Unsupported snapshot version {version!r}")

    have = snapshot.get("snapshot_schema_version")

    if have == version:
        return snapshot

    if have not in SUPPORTED_SNAPSHOT_VERSIONS or int(have) < int(version):
        raise ValueError(
            f"Cannot express a version {have!r} snapshot as version {version!r}"
        )

    if have == "4":
        projected = {k: v for k, v in snapshot.items() if k not in V4_SNAPSHOT_KEYS}
        projected["snapshot_schema_version"] = "3"
        return as_version(projected, version)

    if have == "3":
        projected = {k: v for k, v in snapshot.items() if k not in V3_SNAPSHOT_KEYS}
        projected["snapshot_schema_version"] = "2"
        return as_version(projected, version)

    projected = {k: v for k, v in snapshot.items() if k not in V2_SNAPSHOT_KEYS}
    projected["snapshot_schema_version"] = "1"
    projected["corrections"] = [
        {k: v for k, v in c.items() if k not in V2_CORRECTION_KEYS}
        for c in snapshot["corrections"]
    ]
    return projected


def _check_caller_connection(connection):
    if not connection.in_transaction:
        raise ValueError(
            "A caller-supplied connection must already be in "
            "a consistent-snapshot transaction."
        )

    cursor = connection.cursor(dictionary=True)

    try:
        settings = _fetch(
            cursor,
            "SELECT @@SESSION.transaction_isolation AS isolation, "
            "@@SESSION.time_zone AS time_zone",
        )[0]
    finally:
        cursor.close()

    if settings["isolation"] not in (
        "REPEATABLE-READ", "SERIALIZABLE"
    ):
        raise ValueError(
            "Snapshot requires REPEATABLE READ isolation; "
            f"session uses {settings['isolation']}."
        )

    if settings["time_zone"] not in ("+00:00", "UTC"):
        raise ValueError(
            "Snapshot requires a UTC session time zone; "
            f"session uses {settings['time_zone']}."
        )


def load_evidence_snapshot(
    case_id=1,
    years=(2024, 2025),
    connection=None,
    version=SNAPSHOT_SCHEMA_VERSION,
):
    """
    Read a consistent evidence snapshot for a case, in snapshot
    version `version` (default: the latest).

    Connection ownership:
    - connection=None: opens its own connection, sets the
      session time zone to UTC, reads everything inside one
      read-only REPEATABLE READ transaction with a
      consistent snapshot, rolls it back, and closes it.
    - connection supplied: the caller owns it and must have
      started a consistent-snapshot transaction with a UTC
      session time zone. It is checked, never committed,
      rolled back, or closed here.
    """

    years = tuple(years)

    if version not in SUPPORTED_SNAPSHOT_VERSIONS:
        raise ValueError(f"Unsupported snapshot version {version!r}")

    if not years or not all(
        isinstance(year, int) and not isinstance(year, bool)
        for year in years
    ):
        raise ValueError(
            "years must be a non-empty sequence of integers"
        )

    if connection is not None:
        _check_caller_connection(connection)
        rows = _read(connection, case_id, years, v4=version == "4")

    else:
        owned = _connect()

        try:
            cursor = owned.cursor()
            try:
                cursor.execute("SET SESSION time_zone = '+00:00'")
            finally:
                cursor.close()

            owned.start_transaction(
                consistent_snapshot=True,
                isolation_level="REPEATABLE READ",
                readonly=True,
            )

            try:
                rows = _read(owned, case_id, years, v4=version == "4")
            finally:
                owned.rollback()

        finally:
            owned.close()

    return as_version(_assemble(case_id, years, rows), version)


def snapshot_claims_frame(snapshot):
    """
    Current claims as the string DataFrame expected by
    reconcile_claims() and build_reconciled_comparison().
    """

    records = [
        {
            "claim_id": claim["claim_id"],
            "report_year": claim["report_year"],
            "period_end": claim["period_end"],
            "source_id": claim["source_id"],
            "entity": claim["entity"],
            "metric": claim["metric"],
            "value": claim["value"],
            "normalized_value": claim["normalized_value"],
            "source_location": claim["source_location"],
        }
        for claim in snapshot["claims"]
    ]

    return prepare_claims_frame(records, strict=False)
