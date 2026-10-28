
"""
Restartable GLEIF RR importer (Milestone 23B.2B-2), into the 23B.2B-1
observation schema.

    import_rr_file(connect, path, selection, started_by=...)

1. scan_file(): first pass (SHA-256, header, every record validated and
   counted). Nothing is written if the file is malformed.
2. The file is registered once per (dataset, SHA-256); the batch once per
   (file, selection fingerprint, record hash version). A COMPLETED batch
   is left alone; a FAILED or expired one is resumed under a new lease.
3. Second pass in chunks. Each chunk is one transaction: lock the batch
   row and check the lease, insert missing identity rows, new
   observations with their periods, memberships and absent anchors, then
   progress and lease renewal. Progress never runs ahead of its rows.
4. Before completion the unread rest of the file is hashed and the bytes,
   header, count and file identity must still match the registration.

Writes go only to the isolated test and E2E databases. The lease token in
@crie_gleif_lease fences stale or concurrent workers; it is not an
authentication mechanism. Each import uses its own connection, which is
closed with the lease cleared.
"""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from src import gleif_lei2, gleif_rr
from src.claim_subjects import lei_is_valid
from src.gleif_observations import (
    RECORD_HASH_VERSION,
    batch_coverage,
    canonical_bytes,
    canonical_selection,
    coverage_properties,
    selection_fingerprint,
)
from src.gleif_rr import SourceChangedError, parse_datetime

ALLOWED_DATABASES = ("corporate_intelligence_test", "corporate_intelligence_e2e")
PARSER_VERSION = "gleif_rr-1"
SOURCE_DATASET = "GLEIF RR-CDF"


class ImportRefused(RuntimeError):
    """The import would write somewhere it must not."""


class BatchBusy(RuntimeError):
    """Another importer holds, or has just won, this batch."""


class LeaseLost(RuntimeError):
    """This worker's lease expired or was taken over; it must stop writing."""


class ImportIntegrityError(RuntimeError):
    """Stored rows and the file disagree in a way idempotency cannot explain."""


def selects(selection, ordinal, key):
    """Whether record `ordinal` is selected; key is (child, parent, type) or an LEI."""

    mode = selection["mode"]
    if mode == "ALL_RECORDS":
        return True
    if mode == "FIRST_N":
        return ordinal <= selection["limit"]
    return matched_endpoint(selection, key) is not None


def matched_endpoint(selection, key):
    """For LEI_SET: which endpoint of the relationship the selection matched."""

    if selection["mode"] != "LEI_SET":
        return None
    leis, match = set(selection["leis"]), selection["match"]
    if isinstance(key, str):                                  # an LEI2 record's LEI
        return "ENTITY" if match == "ENTITY" and key in leis else None
    child = key[0] in leis and match in ("CHILD", "CHILD_OR_PARENT")
    parent = key[1] in leis and match in ("PARENT", "CHILD_OR_PARENT")
    return "BOTH" if child and parent else "CHILD" if child else "PARENT" if parent else None


def _execute(cursor, sql, params=()):
    cursor.execute(sql, params)
    return cursor


def _set_lease(cursor, token):
    cursor.execute("SET @crie_gleif_lease = %s", (token,))


def require_isolated_database(connection):
    """Refuse anything but the isolated test and E2E databases."""

    cursor = connection.cursor()
    try:
        database = _execute(cursor, "SELECT DATABASE()").fetchone()[0]
        if database not in ALLOWED_DATABASES:
            raise ImportRefused(f"GLEIF imports may only write to {', '.join(ALLOWED_DATABASES)}; "
                                f"this connection uses {database!r}")
        cursor.execute("SET SESSION time_zone = '+00:00'")
        _set_lease(cursor, None)
    finally:
        cursor.close()
    connection.rollback()
    return database


def require_quarantine_schema(connection):
    """Imports need the m23b2b3 schema (quarantine, positions, header metadata)."""

    cursor = connection.cursor()
    try:
        installed = _execute(cursor, "SELECT COUNT(*) FROM information_schema.TABLES WHERE "
                                     "TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN "
                                     "('gleif_quarantined_records', 'gleif_batch_positions')"
                             ).fetchone()[0] == 2
    finally:
        cursor.close()
        connection.rollback()
    if not installed:
        raise ImportRefused("This database lacks migration m23b2b3 (quarantine and header "
                            "metadata); nothing was imported")


def _header_extension(header):
    """The stored form of the header extension: parsed sources, and the count check."""

    extension = header.get("header_extension")
    if extension is None:
        return None, None
    stored = {"namespace": extension["namespace"], "sources": extension["sources"]}
    return canonical_bytes(stored).decode("utf-8"), extension["source_record_counts_match_header"]


def register_source_file(connection, scan, registered_by, source_reference=None, dataset="RR"):
    """The file's row, created once per (dataset, SHA-256). Returns (id, created)."""

    header = scan["header"]
    extension, consistent = _header_extension(header)
    cursor = connection.cursor(dictionary=True)
    try:
        for attempt in (1, 2):
            row = _execute(cursor, "SELECT * FROM gleif_source_files WHERE dataset = %s "
                                   "AND file_sha256 = %s", (dataset, scan["sha256"])).fetchone()
            connection.rollback()
            if row:
                expected = {"file_size_bytes": scan["identity"]["size"],
                            "record_count": scan["record_count"],
                            "header_record_count": header["header_record_count"],
                            "content_date_text": header["content_date_text"],
                            "publication_scope": header["publication_scope"],
                            "file_content_text": header["file_content_text"],
                            "originator": header.get("originator"),
                            "header_source_counts_consistent": consistent}
                differs = {k: (row[k], v) for k, v in expected.items()
                           if (bool(row[k]) if k == "header_source_counts_consistent"
                               and row[k] is not None else row[k]) != v}
                stored = row["header_extension"]
                if (json.loads(stored) if stored else None) != (json.loads(extension) if extension
                                                                else None):
                    differs["header_extension"] = "differs"
                if differs:
                    raise ImportIntegrityError(f"Registered file {row['source_file_id']} has the same "
                                               f"SHA-256 but different details: {differs}")
                return row["source_file_id"], False
            if attempt == 2:
                break
            try:
                cursor.execute(
                    "INSERT INTO gleif_source_files (dataset, file_sha256, file_size_bytes, "
                    "file_name, content_date_text, content_date_utc, file_content_text, "
                    "publication_scope, delta_basis, header_record_count, record_count, "
                    "source_reference, registered_by, originator, header_extension, "
                    "header_source_counts_consistent) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (dataset, scan["sha256"], scan["identity"]["size"], scan["file_name"],
                     header["content_date_text"], header["content_date_utc"],
                     header["file_content_text"], header["publication_scope"],
                     header["delta_basis"], header["header_record_count"], scan["record_count"],
                     source_reference, registered_by, header.get("originator"), extension,
                     consistent))
                file_id = cursor.lastrowid
                connection.commit()
                return file_id, True
            except Exception as error:
                connection.rollback()
                if getattr(error, "errno", None) != 1062:
                    raise
        raise ImportIntegrityError("The source file could not be registered")
    finally:
        cursor.close()


BATCH_COLUMNS = ("batch_id, source_file_id, selection_mode, selection_limit, status, lease_token, "
                 "attempt, records_scanned, records_selected, observations_inserted, "
                 "observations_already_recorded, quarantined_inserted, "
                 "quarantined_already_recorded, identical_duplicates, failure_reason, "
                 "lease_expires_at_utc > UTC_TIMESTAMP(6) AS lease_live")
COUNTERS = ("records_scanned", "records_selected", "observations_inserted",
            "observations_already_recorded", "quarantined_inserted",
            "quarantined_already_recorded", "identical_duplicates")
OUTCOME_COUNTERS = {"INSERTED": "observations_inserted",
                    "ALREADY_RECORDED": "observations_already_recorded",
                    "QUARANTINED_INSERTED": "quarantined_inserted",
                    "QUARANTINED_ALREADY_RECORDED": "quarantined_already_recorded",
                    "IDENTICAL_DUPLICATE": "identical_duplicates"}


def fetch_batch(connection, batch_id, lock=False):
    cursor = connection.cursor(dictionary=True)
    try:
        return _execute(cursor, f"SELECT {BATCH_COLUMNS} FROM gleif_import_batches "
                                f"WHERE batch_id = %s{' FOR UPDATE' if lock else ''}",
                        (batch_id,)).fetchone()
    finally:
        cursor.close()


def acquire_batch(connection, source_file_id, selection, started_by, lease_seconds):
    """
    Start, resume or take over the batch for this file and selection.
    Returns (batch, token, how); how is STARTED, RESUMED, TAKEN_OVER or
    ALREADY_COMPLETED (token None: nothing will be written).
    """

    fingerprint = selection_fingerprint(selection)
    token = uuid.uuid4().hex
    expiry = int(lease_seconds * 1_000_000)
    cursor = connection.cursor(dictionary=True)
    try:
        row = _execute(cursor, f"SELECT {BATCH_COLUMNS} FROM gleif_import_batches "
                               "WHERE source_file_id = %s AND selection_fingerprint = %s "
                               "AND record_hash_version = %s",
                       (source_file_id, fingerprint, RECORD_HASH_VERSION)).fetchone()
        connection.rollback()
        _set_lease(cursor, token)
        try:
            if row is None:
                cursor.execute(
                    "INSERT INTO gleif_import_batches (source_file_id, selection_mode, "
                    "selection_limit, selection_json, selection_fingerprint, selection_description, "
                    "parser_version, record_hash_version, lease_token, lease_expires_at_utc, "
                    "started_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "UTC_TIMESTAMP(6) + INTERVAL %s MICROSECOND, %s)",
                    (source_file_id, selection["mode"], selection.get("limit"),
                     canonical_bytes(selection).decode(), fingerprint, describe(selection),
                     PARSER_VERSION, RECORD_HASH_VERSION, token, expiry, started_by))
                batch_id, how = cursor.lastrowid, "STARTED"
            elif row["status"] == "COMPLETED":
                return row, None, "ALREADY_COMPLETED"
            elif row["status"] == "STARTED" and row["lease_live"]:
                raise BatchBusy(f"Batch {row['batch_id']} is held by a live lease")
            else:
                cursor.execute(
                    "UPDATE gleif_import_batches SET status = 'STARTED', lease_token = %s, "
                    "lease_expires_at_utc = UTC_TIMESTAMP(6) + INTERVAL %s MICROSECOND, "
                    "attempt = attempt + 1 WHERE batch_id = %s AND status = %s AND lease_token = %s",
                    (token, expiry, row["batch_id"], row["status"], row["lease_token"]))
                if cursor.rowcount != 1:
                    raise BatchBusy(f"Batch {row['batch_id']} changed while acquiring it")
                batch_id = row["batch_id"]
                how = "RESUMED" if row["status"] == "FAILED" else "TAKEN_OVER"
            connection.commit()
        except Exception as error:
            connection.rollback()
            if getattr(error, "errno", None) in (1062, 1644):
                raise BatchBusy(f"Another importer won this batch ({error})") from None
            raise
        return fetch_batch(connection, batch_id), token, how
    finally:
        _set_lease(cursor, None)
        cursor.close()
        connection.rollback()


def describe(selection):
    mode = selection["mode"]
    if mode == "ALL_RECORDS":
        return "Every record of the file"
    if mode == "FIRST_N":
        return f"Sample: the first {selection['limit']} records of the file"
    return (f"Sample: records whose {selection['match'].lower().replace('_', ' ')} is one of "
            f"{len(selection['leis'])} LEI(s)")


def _utc(value):
    """A canonical UTC text (from record_fields) as a naive UTC datetime."""

    return None if value is None else parse_datetime(value, "date")


def _ensure_identity(cursor, record):
    """Insert missing entity and relationship identity rows; never update them."""

    fields = record["fields"]
    for lei in record["key"][:2]:
        if not _execute(cursor, "SELECT lei FROM legal_entities WHERE lei = %s FOR SHARE",
                        (lei,)).fetchone():
            cursor.execute("INSERT INTO legal_entities (lei) VALUES (%s)", (lei,))
    if not _execute(cursor, "SELECT child_lei FROM corporate_relationships WHERE child_lei = %s "
                            "AND parent_lei = %s AND relationship_type = %s FOR SHARE",
                    record["key"]).fetchone():
        # First-seen status, as legacy imports recorded it; later
        # observations never change this row.
        cursor.execute("INSERT INTO corporate_relationships (child_lei, parent_lei, "
                       "relationship_type, relationship_status, registration_status, "
                       "source_dataset) VALUES (%s, %s, %s, %s, %s, %s)",
                       (*record["key"], fields["relationship_status"],
                        fields["registration_status"], SOURCE_DATASET))


def _find_observation(cursor, batch, record):
    row = _execute(cursor, "SELECT observation_id FROM relationship_observations "
                           "WHERE source_file_id = %s AND record_hash_version = %s "
                           "AND child_lei = %s AND parent_lei = %s AND relationship_type = %s "
                           "AND record_sha256 = %s FOR SHARE",
                   (batch["source_file_id"], RECORD_HASH_VERSION, *record["key"],
                    record["record_sha256"])).fetchone()
    return row["observation_id"] if row else None


def _insert_observation(cursor, batch, record, secondary=()):
    f = record["fields"]
    try:
        cursor.execute(
            "INSERT INTO relationship_observations (source_file_id, batch_id, "
            "source_record_ordinal, record_hash_version, record_sha256, child_lei, parent_lei, "
            "relationship_type, relationship_status, registration_status, validation_sources, "
            "validation_documents, validation_reference, start_node_id_type, end_node_id_type, "
            "qualifiers, quantifiers, initial_registration_at_utc, last_update_at_utc, "
            "next_renewal_at_utc, managing_lou, identifier_flags) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (batch["source_file_id"], batch["batch_id"], record["ordinal"], RECORD_HASH_VERSION,
             record["record_sha256"], *record["key"], f["relationship_status"],
             f["registration_status"], f["validation_sources"], f["validation_documents"],
             f["validation_reference"], f["start_node_id_type"], f["end_node_id_type"],
             None if f["qualifiers"] is None else json.dumps(f["qualifiers"]),
             None if f["quantifiers"] is None else json.dumps(f["quantifiers"]),
             _utc(f["initial_registration_at_utc"]), _utc(f["last_update_at_utc"]),
             _utc(f["next_renewal_at_utc"]), f["managing_lou"],
             json.dumps(list(secondary)) if secondary else None))
    except Exception as error:
        if getattr(error, "errno", None) != 1062:
            raise
        existing = _find_observation(cursor, batch, record)
        if existing is not None:      # the same record, recorded concurrently
            return existing, False
        clash = _execute(cursor, "SELECT record_sha256, batch_id FROM relationship_observations "
                                 "WHERE source_file_id = %s AND record_hash_version = %s "
                                 "AND source_record_ordinal = %s FOR SHARE",
                         (batch["source_file_id"], RECORD_HASH_VERSION, record["ordinal"])).fetchone()
        raise ImportIntegrityError(
            f"Record {record['ordinal']} of source file {batch['source_file_id']} is already "
            f"stored with different content ({clash and clash['record_sha256']}, batch "
            f"{clash and clash['batch_id']}) than this file now has ({record['record_sha256']})"
        ) from None
    observation_id = cursor.lastrowid
    for index, period in enumerate(f["periods"] or ()):
        cursor.execute("INSERT INTO relationship_observation_periods (observation_id, "
                       "period_index, period_type, start_text, start_at_utc, end_text, end_at_utc) "
                       "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                       (observation_id, index, period["period_type"], period["start_text"],
                        _utc(period["start_at_utc"]), period["end_text"], _utc(period["end_at_utc"])))
    return observation_id, True


def _ensure_anchor(cursor, batch, record, file_name):
    """
    Create the relationship_evidence anchor for this relationship and the
    time the file's data is valid as of (GLEIF ContentDate) only if none
    exists; an existing anchor (legacy
    or from another file with the same date) is never changed.
    """

    f = record["fields"]
    snapshot = "(SELECT CAST(content_date_utc AS DATETIME) FROM gleif_source_files " \
               "WHERE source_file_id = %s)"
    if _execute(cursor, f"SELECT 1 FROM relationship_evidence WHERE child_lei = %s AND "
                        f"parent_lei = %s AND relationship_type = %s AND snapshot_at_utc = {snapshot} "
                        "FOR SHARE", (*record["key"], batch["source_file_id"])).fetchone():
        return "EXISTING"
    if f["validation_sources"] is None or f["validation_documents"] is None:
        return "NOT_CREATED_UNSTATED_VALIDATION"
    periods = [{"type": p["period_type"], "start": p["start_text"], "end": p["end_text"]}
               for p in f["periods"] or ()]
    cursor.execute(
        "INSERT INTO relationship_evidence (child_lei, parent_lei, relationship_type, "
        "snapshot_at_utc, initial_registration_at_utc, last_update_at_utc, next_renewal_at_utc, "
        "managing_lou, validation_sources, validation_documents, validation_reference, "
        f"periods_json, source_file, first_batch_id) SELECT %s, %s, %s, {snapshot}, "
        "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s",
        (*record["key"], batch["source_file_id"], _utc(f["initial_registration_at_utc"]),
         _utc(f["last_update_at_utc"]), _utc(f["next_renewal_at_utc"]), f["managing_lou"],
         f["validation_sources"], f["validation_documents"], f["validation_reference"],
         json.dumps(periods), file_name, batch["batch_id"]))
    return "CREATED"


ISSUE_CHECK_DIGITS = "IDENTIFIER_FAILS_ISO_17442_CHECK_DIGITS"
ISSUE_PROVISIONAL = "PROVISIONAL_NODE_IDENTIFIER"


def _issue(role, value, node_id_type=None):
    """The problem with one stated identifier, or None if it is a valid LEI."""

    issues = []
    if node_id_type == "ISO_17442_COMPATIBLE":
        issues.append(ISSUE_PROVISIONAL)
    if not lei_is_valid(value):
        issues.append(ISSUE_CHECK_DIGITS)
    if not issues:
        return None
    entry = {"role": role, "value": value, "issue": "+".join(issues)}
    if node_id_type is not None:
        entry["node_id_type"] = node_id_type
    return entry


def rr_identifier_issues(fields):
    """(subject issues -> quarantine, secondary issues -> flags) of an RR record."""

    subject = [i for i in (_issue("StartNode", fields["child_lei"], fields["start_node_id_type"]),
                           _issue("EndNode", fields["parent_lei"], fields["end_node_id_type"])) if i]
    secondary = [i for i in (_issue("ManagingLOU", fields["managing_lou"]),) if i]
    return subject, secondary


def lei2_identifier_issues(fields):
    """(subject issues -> quarantine, secondary issues -> flags) of an LEI2 record."""

    subject = [i for i in (_issue("LEI", fields["lei"]),) if i]
    secondary = [_issue("ManagingLOU", fields["registration"]["managing_lou"])]
    for index, successor in enumerate(fields["successor_entities"] or ()):
        if successor["lei"]:
            secondary.append(_issue(f"SuccessorEntity[{index}]", successor["lei"]))
    if fields["associated_entity"] and fields["associated_entity"]["lei"]:
        secondary.append(_issue("AssociatedEntity", fields["associated_entity"]["lei"]))
    return subject, [i for i in secondary if i]


def _duplicate(cursor, batch, record, repeats_ordinal):
    cursor.execute("INSERT INTO gleif_batch_duplicate_occurrences (batch_id, source_file_id, "
                   "source_record_ordinal, repeats_ordinal, record_sha256) "
                   "VALUES (%s, %s, %s, %s, %s)",
                   (batch["batch_id"], batch["source_file_id"], record["ordinal"],
                    repeats_ordinal, record["record_sha256"]))
    return "IDENTICAL_DUPLICATE"


def write_quarantine(cursor, batch, record, dataset, subject_key, issues):
    """
    Keep a record whose subject identifier cannot be trusted, whole and
    exactly as stated, without any trusted identity row. Returns the
    outcome (QUARANTINED_INSERTED, QUARANTINED_ALREADY_RECORDED or
    IDENTICAL_DUPLICATE).
    """

    lookup = ("SELECT quarantine_id FROM gleif_quarantined_records WHERE source_file_id = %s "
              "AND record_hash_version = %s AND subject_key = %s AND record_sha256 = %s FOR SHARE")
    key = (batch["source_file_id"], RECORD_HASH_VERSION, subject_key, record["record_sha256"])
    found = _execute(cursor, lookup, key).fetchone()
    created = False
    if found is None:
        try:
            cursor.execute(
                "INSERT INTO gleif_quarantined_records (dataset, source_file_id, batch_id, "
                "source_record_ordinal, record_hash_version, record_sha256, subject_key, "
                "canonical_record, identifiers, reason_flags) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (dataset, batch["source_file_id"], batch["batch_id"], record["ordinal"],
                 RECORD_HASH_VERSION, record["record_sha256"], subject_key,
                 canonical_bytes(record["fields"]).decode("utf-8"), json.dumps(issues),
                 json.dumps(sorted({part for i in issues for part in i["issue"].split("+")}))))
            quarantine_id, created = cursor.lastrowid, True
        except Exception as error:
            if getattr(error, "errno", None) != 1062:
                raise
            found = _execute(cursor, lookup, key).fetchone()
            if found is None:
                raise ImportIntegrityError(
                    f"Record {record['ordinal']} of source file {batch['source_file_id']} cannot "
                    "be quarantined: its position already holds different stored content") from None
    if not created:
        quarantine_id = found["quarantine_id"]
    member = _execute(cursor, "SELECT source_record_ordinal FROM gleif_batch_quarantined_records "
                              "WHERE batch_id = %s AND quarantine_id = %s FOR SHARE",
                      (batch["batch_id"], quarantine_id)).fetchone()
    if member:
        return _duplicate(cursor, batch, record, member["source_record_ordinal"])
    cursor.execute("INSERT INTO gleif_batch_quarantined_records (batch_id, quarantine_id, "
                   "source_file_id, source_record_ordinal) VALUES (%s, %s, %s, %s)",
                   (batch["batch_id"], quarantine_id, batch["source_file_id"], record["ordinal"]))
    return "QUARANTINED_INSERTED" if created else "QUARANTINED_ALREADY_RECORDED"


def write_record(cursor, batch, record, file_name):
    """
    One selected RR record. Returns (outcome, anchor). A record whose
    endpoint is provisional or fails check digits is quarantined: no
    identity, relationship, observation or anchor is created from it.
    """

    subject, secondary = rr_identifier_issues(record["fields"])
    if subject:
        return write_quarantine(cursor, batch, record, "RR", "|".join(record["key"]),
                                subject), "NOT_CREATED_QUARANTINED"
    _ensure_identity(cursor, record)
    observation_id = _find_observation(cursor, batch, record)
    created = False
    if observation_id is None:
        observation_id, created = _insert_observation(cursor, batch, record, secondary)
    member = _execute(cursor, "SELECT source_record_ordinal FROM relationship_batch_observations "
                              "WHERE batch_id = %s AND observation_id = %s FOR SHARE",
                      (batch["batch_id"], observation_id)).fetchone()
    if member:
        outcome = _duplicate(cursor, batch, record, member["source_record_ordinal"])
    else:
        cursor.execute("INSERT INTO relationship_batch_observations (batch_id, observation_id, "
                       "source_file_id, source_record_ordinal) VALUES (%s, %s, %s, %s)",
                       (batch["batch_id"], observation_id, batch["source_file_id"], record["ordinal"]))
        outcome = "INSERTED" if created else "ALREADY_RECORDED"
    return outcome, _ensure_anchor(cursor, batch, record, file_name)


def _lei2_identity(cursor, fields):
    """Insert the entity's identity row if missing, with first-seen values; never update."""

    if _execute(cursor, "SELECT lei FROM legal_entities WHERE lei = %s FOR SHARE",
                (fields["lei"],)).fetchone():
        return
    cursor.execute("INSERT INTO legal_entities (lei, legal_name, jurisdiction, country, "
                   "registration_status) VALUES (%s, %s, %s, %s, %s)",
                   (fields["lei"], fields["legal_name"]["text"], fields["legal_jurisdiction"],
                    fields["legal_address"]["country"], fields["registration"]["status"]))


def _lei2_observation(cursor, batch, record, secondary):
    f = record["fields"]
    registration = f["registration"]
    authority = f["registration_authority"] or {}
    names = ([{"kind": "OTHER", **n} for n in f["other_entity_names"] or ()]
             + [{"kind": "TRANSLITERATED", **n} for n in f["transliterated_other_entity_names"] or ()])
    successor_leis = [s["lei"] for s in f["successor_entities"] or () if s["lei"]]
    when = lambda d: _utc(d["utc"]) if d else None                       # noqa: E731
    values = {
        "source_file_id": batch["source_file_id"], "batch_id": batch["batch_id"],
        "source_record_ordinal": record["ordinal"], "record_hash_version": RECORD_HASH_VERSION,
        "record_sha256": record["record_sha256"], "lei": f["lei"],
        "legal_name": f["legal_name"]["text"], "legal_name_language": f["legal_name"]["lang"],
        "other_names": json.dumps(names) if names else None,
        "entity_status": f["entity_status"],
        "legal_form_code": (f["legal_form"] or {}).get("code"),
        "legal_form_other": (f["legal_form"] or {}).get("other"),
        "legal_jurisdiction": f["legal_jurisdiction"],
        "legal_address_country": f["legal_address"]["country"],
        "registration_authority_id": authority.get("id"),
        "registration_authority_entity_id": authority.get("entity_id"),
        # A single stated successor LEI only; several stay in canonical_record.
        "successor_lei": successor_leis[0] if len(successor_leis) == 1 else None,
        "registration_status": registration["status"],
        "initial_registration_at_utc": when(registration["initial_registration"]),
        "last_update_at_utc": when(registration["last_update"]),
        "next_renewal_at_utc": when(registration["next_renewal"]),
        "managing_lou": registration["managing_lou"],
        "canonical_record": canonical_bytes(f).decode("utf-8"),
        "entity_category": f["entity_category"], "entity_sub_category": f["entity_sub_category"],
        "entity_created_at_utc": when(f["entity_creation"]),
        "validation_sources": registration["validation_sources"],
        "other_registration_authority_id": authority.get("other_id"),
        "identifier_check": "SECONDARY_IDENTIFIER_FLAGGED" if secondary else "VALID",
        "identifier_flags": json.dumps(secondary) if secondary else None,
    }
    lookup = ("SELECT observation_id FROM legal_entity_observations WHERE source_file_id = %s "
              "AND record_hash_version = %s AND lei = %s AND record_sha256 = %s FOR SHARE")
    key = (batch["source_file_id"], RECORD_HASH_VERSION, f["lei"], record["record_sha256"])
    found = _execute(cursor, lookup, key).fetchone()
    if found:
        return found["observation_id"], False
    try:
        cursor.execute(f"INSERT INTO legal_entity_observations ({', '.join(values)}) "
                       f"VALUES ({', '.join(['%s'] * len(values))})", tuple(values.values()))
        return cursor.lastrowid, True
    except Exception as error:
        if getattr(error, "errno", None) != 1062:
            raise
        found = _execute(cursor, lookup, key).fetchone()
        if found:                                            # the same record, recorded concurrently
            return found["observation_id"], False
        raise ImportIntegrityError(
            f"Record {record['ordinal']} of source file {batch['source_file_id']} is already "
            "stored with different content than this file now has") from None


def write_lei2_record(cursor, batch, record, file_name):
    """
    One selected LEI2 record. Returns (outcome, None). A record whose own
    LEI fails check digits is quarantined; flagged secondary identifiers
    (managing LOU, successors, associated entity) stay flags on a normal
    observation and never become identity rows.
    """

    subject, secondary = lei2_identifier_issues(record["fields"])
    if subject:
        return write_quarantine(cursor, batch, record, "LEI2", record["key"], subject), None
    _lei2_identity(cursor, record["fields"])
    observation_id, created = _lei2_observation(cursor, batch, record, secondary)
    member = _execute(cursor, "SELECT source_record_ordinal FROM entity_batch_observations "
                              "WHERE batch_id = %s AND observation_id = %s FOR SHARE",
                      (batch["batch_id"], observation_id)).fetchone()
    if member:
        return _duplicate(cursor, batch, record, member["source_record_ordinal"]), None
    cursor.execute("INSERT INTO entity_batch_observations (batch_id, observation_id, "
                   "source_file_id, source_record_ordinal) VALUES (%s, %s, %s, %s)",
                   (batch["batch_id"], observation_id, batch["source_file_id"], record["ordinal"]))
    return ("INSERTED" if created else "ALREADY_RECORDED"), None


class _Run:
    """One worker's hold on one batch, for the duration of an import."""

    def __init__(self, connection, batch, token, scan, selection, lease_seconds, hooks, dataset):
        self.connection, self.batch, self.token = connection, batch, token
        self.scan, self.selection = scan, selection
        self.dataset = DATASETS[selection["dataset"]]
        self.expiry = int(lease_seconds * 1_000_000)
        self.hooks = hooks or {}
        self.chunks = 0
        self.anchors = {"CREATED": 0, "EXISTING": 0, "NOT_CREATED_UNSTATED_VALIDATION": 0,
                        "NOT_CREATED_QUARANTINED": 0}
        self.endpoints = ({"ENTITY": 0} if selection["dataset"] == "LEI2"
                          else {"CHILD": 0, "PARENT": 0, "BOTH": 0})

    def _locked_batch(self, cursor):
        """Lock the batch row; this worker must still hold a live lease."""

        _set_lease(cursor, self.token)
        row = _execute(cursor, f"SELECT {BATCH_COLUMNS} FROM gleif_import_batches "
                               "WHERE batch_id = %s FOR UPDATE",
                       (self.batch["batch_id"],)).fetchone()
        if (row["status"] != "STARTED" or row["lease_token"] != self.token
                or not row["lease_live"]):
            raise LeaseLost(f"Batch {row['batch_id']} is no longer held by this worker "
                            f"(status {row['status']}, lease live: {bool(row['lease_live'])})")
        if row["records_scanned"] != self.batch["records_scanned"]:
            raise LeaseLost(f"Batch {row['batch_id']} progressed elsewhere")
        return row

    def commit_chunk(self, records, last_ordinal):
        """One transaction: lock, rows, counters, progress, renewal."""

        self.connection.rollback()
        cursor = self.connection.cursor(dictionary=True)
        try:
            counts = dict(self._locked_batch(cursor))
            self.stream.check_unchanged()
            for record in records:
                if not selects(self.selection, record["ordinal"], record["key"]):
                    continue
                outcome, anchor = self.dataset["writer"](cursor, self.batch, record,
                                                         self.scan["file_name"])
                counts["records_selected"] += 1
                counts[OUTCOME_COUNTERS[outcome]] += 1
                if anchor:
                    self.anchors[anchor] += 1
                endpoint = matched_endpoint(self.selection, record["key"])
                if endpoint:
                    self.endpoints[endpoint] += 1
            cursor.execute(
                "UPDATE gleif_import_batches SET records_scanned = %s, records_selected = %s, "
                "observations_inserted = %s, observations_already_recorded = %s, "
                "quarantined_inserted = %s, quarantined_already_recorded = %s, "
                "identical_duplicates = %s, progress_at_utc = UTC_TIMESTAMP(6), "
                "lease_expires_at_utc = UTC_TIMESTAMP(6) + INTERVAL %s MICROSECOND "
                "WHERE batch_id = %s AND lease_token = %s",
                (last_ordinal, *(counts[c] for c in COUNTERS[1:]),
                 self.expiry, self.batch["batch_id"], self.token))
            if cursor.rowcount != 1:
                raise LeaseLost("The batch progress update was refused")
            self.chunks += 1
            if "before_commit" in self.hooks:
                self.hooks["before_commit"](self.chunks)
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            _set_lease(cursor, None)
            cursor.close()
        self.batch = fetch_batch(self.connection, self.batch["batch_id"])
        self.connection.rollback()
        if "after_commit" in self.hooks:
            self.hooks["after_commit"](self.chunks)

    def complete(self):
        self.connection.rollback()
        cursor = self.connection.cursor(dictionary=True)
        try:
            self._locked_batch(cursor)
            cursor.execute("UPDATE gleif_import_batches SET status = 'COMPLETED', "
                           "completed_at_utc = UTC_TIMESTAMP(6) WHERE batch_id = %s "
                           "AND lease_token = %s", (self.batch["batch_id"], self.token))
            if cursor.rowcount != 1:
                raise LeaseLost("The batch could not be completed")
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            _set_lease(cursor, None)
            cursor.close()

    def fail(self, reason):
        """Mark the batch FAILED if this worker still holds it; never raises."""

        try:
            self.connection.rollback()
            cursor = self.connection.cursor()
            try:
                _set_lease(cursor, self.token)
                cursor.execute("UPDATE gleif_import_batches SET status = 'FAILED', "
                               "failed_at_utc = UTC_TIMESTAMP(6), failure_reason = %s "
                               "WHERE batch_id = %s AND status = 'STARTED' AND lease_token = %s",
                               (reason[:2000], self.batch["batch_id"], self.token))
                self.connection.commit()
                return cursor.rowcount == 1
            finally:
                _set_lease(cursor, None)
                cursor.close()
        except Exception:  # noqa: BLE001 - the original error is what matters
            self.connection.rollback()
            return False

    def run(self, path, chunk_size):
        """Scan from the committed position to the end of the selection."""

        limit = self.scan["record_count"]
        if self.selection["mode"] == "FIRST_N":
            limit = min(self.selection["limit"], limit)
        with self.dataset["stream"](path, self.scan) as self.stream:
            pending = []
            if self.batch["records_scanned"] < limit:
                for record in self.stream:
                    if record["ordinal"] <= self.batch["records_scanned"]:
                        continue
                    pending.append(record)
                    if len(pending) == chunk_size or record["ordinal"] == limit:
                        self.commit_chunk(pending, record["ordinal"])
                        pending = []
                    if record["ordinal"] == limit:
                        break
            if self.batch["records_scanned"] != limit:
                raise SourceChangedError("The file ended before the selection was complete")
            # Bytes FIRST_N did not parse are still hashed before completion.
            self.stream.verify()
            self.complete()


def _import(dataset, connect, path, selection, started_by, registered_by, source_reference,
            chunk_size, lease_seconds, hooks):
    if not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be between 1 and 3600")
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    if selection["dataset"] != dataset:
        raise ValueError(f"Only {dataset} selections are supported by this importer")
    path = os.fspath(path)
    scan = DATASETS[dataset]["scan"](path)           # before any connection or write

    connection = connect()
    run = None
    try:
        database = require_isolated_database(connection)
        require_quarantine_schema(connection)
        source_file_id, registered = register_source_file(
            connection, scan, registered_by or started_by, source_reference, dataset)
        batch, token, how = acquire_batch(connection, source_file_id, selection, started_by,
                                          lease_seconds)
        if token is not None:
            run = _Run(connection, batch, token, scan, selection, lease_seconds, hooks, dataset)
            try:
                run.run(path, chunk_size)
            except Exception as error:
                run.fail(f"{type(error).__name__}: {error}")
                raise
        final = fetch_batch(connection, batch["batch_id"])
        connection.rollback()
        quarantined = final["quarantined_inserted"] + final["quarantined_already_recorded"]
        return {
            "database": database, "dataset": dataset, "source_file_id": source_file_id,
            "source_file_registered": registered, "source_sha256": scan["sha256"],
            "batch_id": final["batch_id"], "acquired": how, "status": final["status"],
            "attempt": final["attempt"],
            "coverage": batch_coverage(final["status"], final["selection_mode"],
                                       scan["header"]["publication_scope"], quarantined),
            "coverage_properties": coverage_properties(
                final["status"], final["selection_mode"], scan["header"]["publication_scope"],
                quarantined),
            "counts": {k: final[k] for k in COUNTERS},
            "chunks_committed": run.chunks if run else 0,
            "anchors": run.anchors if run and dataset == "RR" else None,
            "matched_endpoints": run.endpoints if run and selection["mode"] == "LEI_SET" else None,
        }
    finally:
        try:
            connection.rollback()
            cursor = connection.cursor()
            _set_lease(cursor, None)
            cursor.close()
        finally:
            connection.close()


def import_rr_file(connect, path, selection, *, started_by, registered_by=None,
                   source_reference=None, chunk_size=500, lease_seconds=300, hooks=None):
    """
    Import one RR file for one selection (from canonical_selection). Safe
    to call again after any interruption: a completed batch is reported
    and left unchanged, an interrupted one resumes from its committed
    position. Returns a summary of the batch.
    """

    return _import("RR", connect, path, selection, started_by, registered_by, source_reference,
                   chunk_size, lease_seconds, hooks)


def import_lei2_file(connect, path, selection, *, started_by, registered_by=None,
                     source_reference=None, chunk_size=500, lease_seconds=300, hooks=None):
    """Import one LEI2 (LEI-CDF) file for one selection; as import_rr_file."""

    return _import("LEI2", connect, path, selection, started_by, registered_by, source_reference,
                   chunk_size, lease_seconds, hooks)


DATASETS = {
    "RR": {"scan": gleif_rr.scan_file, "stream": gleif_rr.RecordStream, "writer": write_record},
    "LEI2": {"scan": gleif_lei2.scan_file, "stream": gleif_lei2.RecordStream,
             "writer": write_lei2_record},
}


def _connect_to(database):
    import mysql.connector
    from dotenv import load_dotenv

    if database not in ALLOWED_DATABASES:
        raise ImportRefused(f"--database must be one of {', '.join(ALLOWED_DATABASES)}")
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    return lambda: mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"), port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"), password=os.getenv("MYSQL_PASSWORD"), database=database)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Import one GLEIF RR or LEI2 file "
                                                 "(test and E2E databases only).")
    parser.add_argument("path")
    parser.add_argument("--dataset", choices=("RR", "LEI2"), default="RR")
    parser.add_argument("--database", required=True, choices=ALLOWED_DATABASES)
    parser.add_argument("--mode", required=True, choices=("ALL_RECORDS", "LEI_SET", "FIRST_N"))
    parser.add_argument("--lei", action="append", default=[])
    parser.add_argument("--match", choices=("CHILD", "PARENT", "CHILD_OR_PARENT", "ENTITY"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--started-by", required=True)
    parser.add_argument("--source-reference")
    parser.add_argument("--chunk-size", type=int, default=500)
    args = parser.parse_args(argv)

    selection = canonical_selection(args.dataset, args.mode, leis=args.lei or None,
                                    match=args.match, limit=args.limit)
    importer = import_lei2_file if args.dataset == "LEI2" else import_rr_file
    summary = importer(_connect_to(args.database), args.path, selection,
                             started_by=args.started_by, source_reference=args.source_reference,
                             chunk_size=args.chunk_size)
    json.dump(summary, sys.stdout, indent=2, default=str)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
