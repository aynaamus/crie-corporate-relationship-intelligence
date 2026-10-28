
"""
Historical GLEIF observations (Milestone 23B.2B-1): the identity, hashing
and lifecycle rules that sql/migrations/m23b2b_gleif_observations.sql
enforces, as pure functions the importer (23B.2B-2) and the history
readers (23B.2B-3) share. Nothing here touches a database or a file.

A source file is identified by its dataset and SHA-256 alone; a batch by
that file, a selection fingerprint and the record hash version; an
observation by its file, hash version, subject and record hash.
"""

import hashlib
import json
import re

SELECTION_VERSION = 1
RECORD_HASH_VERSION = 1

DATASETS = ("RR", "LEI2")
PUBLICATION_SCOPES = ("FULL", "DELTA", "UNKNOWN")
SELECTION_MODES = ("ALL_RECORDS", "LEI_SET", "FIRST_N")
LEI_MATCHES = ("CHILD_OR_PARENT", "CHILD", "PARENT", "ENTITY")

NOT_CAPTURED = "NOT CAPTURED"
NOT_STATED = "NOT STATED"

# Longest a lease may run ahead of the server clock; holders renew it with
# each progress update.
LEASE_MAX_SECONDS = 3600

# (old status, new status) a batch row may take; None is the insert.
TRANSITIONS = {
    (None, "STARTED"),
    ("STARTED", "STARTED"),      # progress (holder) or takeover (after expiry)
    ("STARTED", "FAILED"),       # holder, or anyone after expiry
    ("FAILED", "STARTED"),       # resume under a new lease
    ("STARTED", "COMPLETED"),    # holder, selection complete
}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LEI = re.compile(r"^[0-9A-Z]{18}[0-9]{2}$")


class ObservationError(ValueError):
    """A selection, record or transition that the rules do not allow."""


def canonical_bytes(value):
    """Canonical JSON: sorted keys, no whitespace, exact Unicode text."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def canonical_selection(dataset, mode, leis=None, match=None, limit=None):
    """The deterministic description of which records of a file to import."""

    if dataset not in DATASETS:
        raise ObservationError(f"Unknown dataset: {dataset!r}")
    if mode not in SELECTION_MODES:
        raise ObservationError(f"Unknown selection mode: {mode!r}")
    selection = {"selection_version": SELECTION_VERSION, "dataset": dataset, "mode": mode}

    if mode == "LEI_SET":
        cleaned = sorted({str(lei).strip().upper() for lei in leis or ()})
        if not cleaned or not all(_LEI.match(lei) for lei in cleaned):
            raise ObservationError("An LEI_SET selection needs well-formed LEIs")
        allowed = ("ENTITY",) if dataset == "LEI2" else LEI_MATCHES[:3]
        if match not in allowed:
            raise ObservationError(f"LEI match for {dataset} must be one of {allowed}")
        selection.update(leis=cleaned, match=match)
    elif leis or match:
        raise ObservationError(f"{mode} takes no LEIs")

    if mode == "FIRST_N":
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ObservationError("A FIRST_N selection needs a positive limit")
        selection["limit"] = limit
    elif limit is not None:
        raise ObservationError(f"{mode} takes no limit")
    return selection


def selection_fingerprint(selection):
    return hashlib.sha256(canonical_bytes(selection)).hexdigest()


def is_sample(selection):
    return selection["mode"] != "ALL_RECORDS"


def _check_json_types(value):
    """
    Canonical records hold only text, integers, booleans, null, lists and
    string-keyed mappings, so storing them as MySQL JSON and reading them
    back reproduces the same canonical bytes. Floats (whose JSON text is
    not stable) and other types are refused.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, list):
        for item in value:
            _check_json_types(item)
    elif isinstance(value, dict) and all(isinstance(k, str) for k in value):
        for item in value.values():
            _check_json_types(item)
    else:
        raise ObservationError(f"Canonical records cannot hold {type(value).__name__} values")


def record_sha256(record):
    """
    Hash of one parsed source record (a dict of the stated fields, None for
    not stated). Field order never matters; text is hashed exactly, so a
    change of case, spacing or Unicode form is a different record.
    """

    if not isinstance(record, dict) or not record:
        raise ObservationError("A record is a non-empty mapping of its fields")
    _check_json_types(record)
    return hashlib.sha256(
        canonical_bytes({"record_hash_version": RECORD_HASH_VERSION, "record": record})
    ).hexdigest()


def check_transition(old, new):
    if (old, new) not in TRANSITIONS:
        raise ObservationError(f"Batch status {old} -> {new} is not permitted")


def batch_counts_consistent(batch):
    """The invariant every committed chunk and every completed batch keeps."""

    return (batch["records_selected"] <= batch["records_scanned"]
            and batch["observations_inserted"] + batch["observations_already_recorded"]
            + batch["identical_duplicates"] == batch["records_selected"]
            and (batch["selection_mode"] == "LEI_SET"
                 or batch["records_selected"] == batch["records_scanned"])
            and (batch.get("selection_limit") is None
                 or batch["records_scanned"] <= batch["selection_limit"]))


def selection_complete(mode, records_scanned, record_count, limit=None):
    """
    Whether a batch has read everything its selection needs: the whole
    file for ALL_RECORDS and LEI_SET (a matching record can be anywhere),
    only the first LEAST(limit, record_count) records for FIRST_N.
    """

    if mode == "FIRST_N":
        return records_scanned == min(limit, record_count)
    if mode in ("ALL_RECORDS", "LEI_SET"):
        return records_scanned == record_count
    raise ObservationError(f"Unknown selection mode: {mode!r}")


def batch_coverage(status, mode, publication_scope, quarantined_records=0):
    """
    A single summary label for a batch. Only a completed ALL_RECORDS import
    of a FULL file with no quarantined records is a complete, trusted copy
    of the published dataset; a sample stays a sample whatever it holds.
    Use coverage_properties() for the three properties separately.
    """

    if status != "COMPLETED":
        return "INCOMPLETE_IMPORT"
    if mode != "ALL_RECORDS":
        return "COMPLETE_SAMPLE_OF_FILE"
    if quarantined_records:
        return "COMPLETE_FILE_SCAN_WITH_QUARANTINED_RECORDS"
    return {"FULL": "COMPLETE_PUBLISHED_DATASET", "DELTA": "COMPLETE_DELTA_FILE"}.get(
        publication_scope, "COMPLETE_FILE_SCOPE_UNKNOWN")


def coverage_properties(status, mode, publication_scope, quarantined_records=0):
    """
    The independent properties of a batch: which records it selected, what
    the file itself covers, and whether any record was quarantined (with
    the exact count). No property implies another.
    """

    if mode not in SELECTION_MODES or publication_scope not in PUBLICATION_SCOPES:
        raise ObservationError("Unknown selection mode or publication scope")
    if not isinstance(quarantined_records, int) or quarantined_records < 0:
        raise ObservationError("quarantined_records must be a non-negative count")
    return {
        "status": status,
        "selection": mode,
        "selection_is_sample": mode != "ALL_RECORDS",
        "source_scope": publication_scope,
        "quarantined_records": quarantined_records,
        "integrity": "QUARANTINE_PRESENT" if quarantined_records else "NO_QUARANTINE",
        "trusted_complete_dataset": (status == "COMPLETED" and mode == "ALL_RECORDS"
                                     and publication_scope == "FULL" and not quarantined_records),
        "label": batch_coverage(status, mode, publication_scope, quarantined_records),
    }


def classify_records(records):
    """
    Group one file's parsed records by subject key. Returns, per key, the
    distinct record hashes in first-seen order with their ordinals, so a
    repeat with identical content is one observation and a conflicting
    repeat is a separate observation, never discarded.
    """

    by_key = {}
    for ordinal, key, record in records:
        digest = record_sha256(record)
        seen = by_key.setdefault(key, {})
        seen.setdefault(digest, []).append(ordinal)
    return {
        key: {
            "observations": [{"record_sha256": d, "source_record_ordinal": o[0],
                              "identical_repeats": len(o) - 1} for d, o in seen.items()],
            "conflicting": len(seen) > 1,
        }
        for key, seen in by_key.items()
    }


def absence_meaning(publication_scope, selection, subject_in_selection=True,
                    quarantined_records=0):
    """
    What it means that a relationship or entity is missing from a batch.
    Absence is never evidence that a relationship ended; at most it is
    "not published in a complete dataset", which a reader must still not
    treat as a termination.
    """

    if publication_scope not in PUBLICATION_SCOPES:
        raise ObservationError(f"Unknown publication scope: {publication_scope!r}")
    if is_sample(selection) and not (selection["mode"] == "LEI_SET" and subject_in_selection):
        return "ABSENT_FROM_SAMPLE"
    if publication_scope == "DELTA":
        return "ABSENT_FROM_DELTA"
    if publication_scope == "UNKNOWN":
        return "ABSENT_SCOPE_UNKNOWN"
    if quarantined_records:
        # A quarantined record may be this subject under an identifier that
        # cannot be trusted: absence cannot be determined.
        return "ABSENCE_UNDETERMINED_QUARANTINE_PRESENT"
    return "NOT_PUBLISHED_IN_FULL_DATASET"


def stated_end(observation):
    """
    Whether the record itself says the relationship or registration is no
    longer current. Only stated values count; this is still a GLEIF
    statement, not an evidentially established termination.
    """

    if observation.get("relationship_status") == "INACTIVE":
        return "REPORTED_INACTIVE"
    registration = observation.get("registration_status")
    if registration in ("RETIRED", "ANNULLED", "DUPLICATE"):
        return f"REPORTED_{registration}"
    return None


LEGACY_FIELDS = ("relationship_status", "registration_status", "qualifiers", "quantifiers",
                 "start_node_id_type", "end_node_id_type", "source_file_sha256", "selection")


def legacy_observation(anchor):
    """
    A pre-23B.2B relationship_evidence row (first_batch_id NULL) as an
    observation. What the legacy import kept is reported as recorded; the
    rest is NOT CAPTURED and is never filled from other observations or
    from corporate_relationships' first-seen status.
    """

    if anchor.get("first_batch_id") is not None:
        raise ObservationError("Not a legacy anchor")
    observation = {
        "origin": "LEGACY_IMPORT",
        "child_lei": anchor["child_lei"], "parent_lei": anchor["parent_lei"],
        "relationship_type": anchor["relationship_type"],
        # GLEIF ContentDate: the time the file's data is valid as of. It is
        # not a publication time; publication_time_utc is kept only as a
        # deprecated alias for existing readers.
        "data_valid_as_of_utc": anchor["snapshot_at_utc"],
        "publication_time_utc": anchor["snapshot_at_utc"],
        "source_file": anchor["source_file"],
        "validation_sources": anchor["validation_sources"],
        "validation_documents": anchor["validation_documents"],
    }
    observation.update({field: NOT_CAPTURED for field in LEGACY_FIELDS})
    return observation


def check_sha256(value):
    if not isinstance(value, str) or not _SHA256.match(value):
        raise ObservationError("Expected a lowercase SHA-256 hex digest")
    return value
