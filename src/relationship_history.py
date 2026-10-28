
"""
Read-only history of GLEIF entity and relationship observations
(Milestone 23B.2B-3c). Nothing here writes.

What a history shows, and what it does not:
  * Each entry is one published observation: a record in a source file
    that at least one COMPLETED import batch includes. Observations seen
    only by STARTED or FAILED batches, and quarantined records, are never
    entries; they are only counted in the audit section.
  * Entries are ordered by the file's data-valid-as-of time (GLEIF
    ContentDate), which is not a publication date and not the date of any
    corporate event. Relationship periods and stated legal-entity events
    are reported exactly as the record states them.
  * A difference between entries means the published records differ. It
    never establishes that, or when, a corporate change occurred; every
    comparison says so. All entries come from GLEIF (self-reported,
    LOU-validated data): repeated observations are not independent
    corroboration.
  * An accounting-consolidation relationship is not evidence of direct
    ownership or managerial control.
  * Legacy rows (imported before 23B.2B) keep NOT CAPTURED for what they
    never recorded; nothing is filled in from other observations.
"""

import json

from src.gleif_observations import NOT_CAPTURED, absence_meaning, batch_coverage

NOT_STATED = "NOT STATED"

SOURCE_NOTE = ("All observations come from GLEIF: data reported by the entity and validated "
               "by its LEI issuer. Repeated observations, in one file or many, are not "
               "independent corroboration.")
CHANGE_NOTE = ("The published records differ between these observations. This does not "
               "establish that, or when, a corporate change occurred; the data-valid-as-of "
               "time of a file is not an event date.")
CONSOLIDATION_NOTE = ("An accounting-consolidation relationship is reported for financial "
                      "reporting. It is not evidence of direct ownership or managerial control.")

ENTITY_FIELDS = {
    "legal_name": lambda f: f["legal_name"],
    "other_entity_names": lambda f: f["other_entity_names"],
    "transliterated_other_entity_names": lambda f: f["transliterated_other_entity_names"],
    "entity_status": lambda f: f["entity_status"],
    "legal_jurisdiction": lambda f: f["legal_jurisdiction"],
    "legal_address_country": lambda f: f["legal_address"]["country"],
    "headquarters_country": lambda f: f["headquarters_address"]["country"],
    "legal_form": lambda f: f["legal_form"],
    "entity_category": lambda f: f["entity_category"],
    "entity_sub_category": lambda f: f["entity_sub_category"],
    "registration_authority": lambda f: f["registration_authority"],
    "successor_entities": lambda f: f["successor_entities"],
    "registration_status": lambda f: f["registration"]["status"],
    "managing_lou": lambda f: f["registration"]["managing_lou"],
    "validation_sources": lambda f: f["registration"]["validation_sources"],
}
RELATIONSHIP_FIELDS = ("relationship_status", "registration_status", "validation_sources",
                       "validation_documents", "validation_reference", "start_node_id_type",
                       "end_node_id_type", "periods", "qualifiers", "quantifiers", "managing_lou")


def _label(entry):
    return f"{entry['source']} (data valid as of {entry['data_valid_as_of_utc']})"


def compare(entries, fields):
    """
    Compare `fields` across `entries` (already in valid-as-of order; each
    entry has "values": {field: value}). Per field:
      CONSISTENT          the same value in every entry that states it
      DIFFERS             at least two different stated values
      SINGLE_OBSERVATION  stated by one entry only
      NOT_STATED          no entry states it
      NOT_CAPTURED        only legacy entries, which never recorded it
    NOT CAPTURED values are unknown: they never count as a difference.
    """

    result = {}
    for field in fields:
        stated = [(e, e["values"].get(field)) for e in entries
                  if e["values"].get(field) not in (None, NOT_CAPTURED)]
        if not stated:
            captured = any(e["values"].get(field) != NOT_CAPTURED for e in entries)
            result[field] = {"status": "NOT_STATED" if captured else "NOT_CAPTURED",
                             "entries_stating": 0}
            continue
        distinct = []
        for _, value in stated:
            if value not in distinct:
                distinct.append(value)
        entry = {"entries_stating": len(stated),
                 "entries_not_stating": len(entries) - len(stated),
                 "values": distinct}
        if len(stated) == 1:
            entry["status"] = "SINGLE_OBSERVATION"
        elif len(distinct) == 1:
            entry["status"] = "CONSISTENT"
        else:
            entry["status"] = "DIFFERS"
            entry["differences"] = [
                {"from": _label(a), "to": _label(b), "before": va, "after": vb,
                 "same_valid_as_of": a["data_valid_as_of_utc"] == b["data_valid_as_of_utc"]}
                for (a, va), (b, vb) in zip(stated, stated[1:]) if va != vb]
            entry["note"] = CHANGE_NOTE
        result[field] = entry
    return result


def assessment(comparison, entries):
    """The overall reading, which never claims a corporate change."""

    differing = sorted(f for f, c in comparison.items() if c["status"] == "DIFFERS")
    unknown = sorted(f for f, c in comparison.items()
                     if c["status"] in ("NOT_STATED", "NOT_CAPTURED", "SINGLE_OBSERVATION"))
    return {
        "published_observations": len(entries),
        "distinct_source_files": len({e["source_file_id"] for e in entries
                                      if e.get("source_file_id") is not None}),
        "consistent_fields": sorted(f for f, c in comparison.items() if c["status"] == "CONSISTENT"),
        "differing_fields": differing,
        "unknown_or_single_fields": unknown,
        # GLEIF records alone cannot show whether a corporate event happened.
        "corporate_change_determination": "INSUFFICIENT_EVIDENCE",
        "note": (CHANGE_NOTE if differing else
                 "No differences between the published records were found; this does not "
                 "establish that nothing changed between or after them.") + " " + SOURCE_NOTE,
    }


def stated_events(entries):
    """Legal-entity events as each record states them; never verified or inferred."""

    events = []
    for entry in entries:
        for event in entry.get("events") or ():
            events.append({"source": _label(entry), "type": event["type"],
                           "event_status": event["event_status"],
                           "effective_as_stated": event["effective"]["text"] if event["effective"] else None,
                           "recorded_as_stated": event["recorded"]["text"],
                           "note": "Stated by the GLEIF record; not verified by CRIE."})
    return events


CONSOLIDATION_TYPES = {"IS_DIRECTLY_CONSOLIDATED_BY", "IS_ULTIMATELY_CONSOLIDATED_BY"}


def _schema(cursor):
    cursor.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()")
    tables = {row[0] if not isinstance(row, dict) else list(row.values())[0]
              for row in cursor.fetchall()}
    cursor.execute("SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                   "AND TABLE_NAME = 'relationship_evidence' AND COLUMN_NAME = 'first_batch_id'")
    row = cursor.fetchone()
    first_batch = (list(row.values())[0] if isinstance(row, dict) else row[0]) == 1
    return tables, first_batch


def _when(value):
    return value.isoformat(timespec="milliseconds") if value is not None else NOT_CAPTURED


def _published(cursor, sql, params):
    """Rows of observations included by at least one COMPLETED batch, grouped."""

    cursor.execute(sql, params)
    grouped = {}
    for row in cursor.fetchall():
        entry = grouped.setdefault(row["observation_id"], {**row, "covering_batches": []})
        entry["covering_batches"].append({
            "batch_id": row["batch_id"], "selection_mode": row["selection_mode"],
            "coverage": batch_coverage("COMPLETED", row["selection_mode"], row["publication_scope"],
                                       int(row["quarantined"] or 0))})
    return list(grouped.values())


def _base_entry(row):
    return {"origin": "GLEIF_OBSERVATION", "observation_id": row["observation_id"],
            "source": f"file {row['file_name']} (source file #{row['source_file_id']})",
            "source_file_id": row["source_file_id"], "file_sha256": row["file_sha256"],
            "data_valid_as_of_utc": _when(row["content_date_utc"]),
            "publication_scope": row["publication_scope"],
            "source_record_ordinal": row["source_record_ordinal"],
            "covering_batches": row["covering_batches"],
            "identifier_flags": json.loads(row["identifier_flags"]) if row["identifier_flags"] else None}


_COVERING = ("JOIN gleif_import_batches b ON b.batch_id = m.batch_id AND b.status = 'COMPLETED' "
             "JOIN gleif_source_files f ON f.source_file_id = o.source_file_id")
_BATCH_COLUMNS = ("b.batch_id, b.selection_mode, "
                  "b.quarantined_inserted + b.quarantined_already_recorded AS quarantined, "
                  "f.file_name, f.file_sha256, f.content_date_utc, f.publication_scope")


def _audit(cursor, tables, dataset, subject_key, observation_table, membership_table, where, params):
    """What is not history: incomplete attempts and quarantined records (counts only)."""

    audit = {"observations_only_in_incomplete_batches": 0, "quarantined_records": 0}
    if observation_table in tables:
        cursor.execute(
            f"SELECT COUNT(*) AS n FROM {observation_table} o WHERE {where} AND NOT EXISTS ("
            f"SELECT 1 FROM {membership_table} m JOIN gleif_import_batches b ON b.batch_id = m.batch_id "
            "WHERE m.observation_id = o.observation_id AND b.status = 'COMPLETED')", params)
        audit["observations_only_in_incomplete_batches"] = cursor.fetchone()["n"]
    if "gleif_quarantined_records" in tables:
        cursor.execute("SELECT COUNT(*) AS n FROM gleif_quarantined_records WHERE dataset = %s "
                       "AND subject_key = %s", (dataset, subject_key))
        audit["quarantined_records"] = cursor.fetchone()["n"]
    audit["note"] = ("Incomplete import attempts and quarantined records are not published "
                     "history and are never compared.")
    return audit


def entity_history(connection, lei):
    """The published GLEIF history of one legal entity (read-only)."""

    cursor = connection.cursor(dictionary=True)
    try:
        tables, _ = _schema(cursor)
        entries = []
        if "legal_entity_observations" in tables:
            rows = _published(cursor, (
                f"SELECT o.observation_id, o.source_file_id, o.source_record_ordinal, "
                f"o.canonical_record, o.identifier_flags, f.source_file_id AS file_id, {_BATCH_COLUMNS} "
                "FROM legal_entity_observations o JOIN entity_batch_observations m "
                f"ON m.observation_id = o.observation_id {_COVERING} WHERE o.lei = %s "
                "ORDER BY f.content_date_utc, f.source_file_id, o.source_record_ordinal, b.batch_id"),
                (lei,))
            for row in rows:
                fields = json.loads(row["canonical_record"])
                entries.append({**_base_entry(row),
                                "values": {name: get(fields) for name, get in ENTITY_FIELDS.items()},
                                "events": fields["legal_entity_events"]})
        cursor.execute("SELECT lei, legal_name, jurisdiction, country, registration_status "
                       "FROM legal_entities WHERE lei = %s", (lei,))
        identity = cursor.fetchone()
        audit = _audit(cursor, tables, "LEI2", lei, "legal_entity_observations",
                       "entity_batch_observations", "o.lei = %s", (lei,))
    finally:
        cursor.close()
        connection.rollback()

    comparison = compare(entries, ENTITY_FIELDS)
    return {
        "subject": {"lei": lei},
        "entries": entries,
        # The identity row: first-seen values of mixed legacy provenance
        # (sample file and API enrichment), with no observation date.
        "identity_row": None if identity is None else {
            **identity, "observed_as_of": NOT_CAPTURED,
            "note": "Identity row, not an observation; never compared with the history."},
        "comparison": comparison,
        "assessment": assessment(comparison, entries),
        "stated_events": stated_events(entries),
        "audit": audit,
    }


def _observation_periods(cursor, observation_id):
    cursor.execute("SELECT period_type, start_text, end_text FROM relationship_observation_periods "
                   "WHERE observation_id = %s ORDER BY period_index", (observation_id,))
    rows = cursor.fetchall()
    return [{"type": r["period_type"], "start": r["start_text"], "end": r["end_text"]}
            for r in rows] or None


def relationship_history(connection, child_lei, parent_lei, relationship_type):
    """The published GLEIF history of one relationship (read-only)."""

    key = (child_lei, parent_lei, relationship_type)
    cursor = connection.cursor(dictionary=True)
    try:
        tables, first_batch = _schema(cursor)
        entries = []
        if "relationship_observations" in tables:
            rows = _published(cursor, (
                "SELECT o.observation_id, o.source_file_id, o.source_record_ordinal, "
                "o.relationship_status, o.registration_status, o.validation_sources, "
                "o.validation_documents, o.validation_reference, o.start_node_id_type, "
                "o.end_node_id_type, o.qualifiers, o.quantifiers, o.managing_lou, "
                f"o.identifier_flags, {_BATCH_COLUMNS} FROM relationship_observations o "
                f"JOIN relationship_batch_observations m ON m.observation_id = o.observation_id "
                f"{_COVERING} WHERE o.child_lei = %s AND o.parent_lei = %s AND o.relationship_type = %s "
                "ORDER BY f.content_date_utc, f.source_file_id, o.source_record_ordinal, b.batch_id"),
                key)
            for row in rows:
                values = {name: row[name] for name in RELATIONSHIP_FIELDS
                          if name not in ("periods", "qualifiers", "quantifiers")}
                values["qualifiers"] = json.loads(row["qualifiers"]) if row["qualifiers"] else None
                values["quantifiers"] = json.loads(row["quantifiers"]) if row["quantifiers"] else None
                values["periods"] = _observation_periods(cursor, row["observation_id"])
                entries.append({**_base_entry(row), "values": values})
        # Legacy anchors (imported before 23B.2B): what they never recorded
        # stays NOT CAPTURED. Anchors created by batches are not repeated:
        # their observations are already entries.
        cursor.execute(
            "SELECT snapshot_at_utc, source_file, validation_sources, validation_documents, "
            "validation_reference, managing_lou, periods_json FROM relationship_evidence "
            "WHERE child_lei = %s AND parent_lei = %s AND relationship_type = %s"
            + (" AND first_batch_id IS NULL" if first_batch else ""), key)
        for row in cursor.fetchall():
            entries.append({
                "origin": "LEGACY_IMPORT", "source": f"legacy import of {row['source_file']}",
                "source_file_id": None, "data_valid_as_of_utc": _when(row["snapshot_at_utc"]),
                "values": {**{name: NOT_CAPTURED for name in RELATIONSHIP_FIELDS},
                           "validation_sources": row["validation_sources"],
                           "validation_documents": row["validation_documents"],
                           "validation_reference": row["validation_reference"],
                           "managing_lou": row["managing_lou"],
                           "periods": json.loads(row["periods_json"]) or None}})
        absences = _absences(cursor, tables, key)
        audit = _audit(cursor, tables, "RR", "|".join(key), "relationship_observations",
                       "relationship_batch_observations",
                       "o.child_lei = %s AND o.parent_lei = %s AND o.relationship_type = %s", key)
    finally:
        cursor.close()
        connection.rollback()

    entries.sort(key=lambda e: (e["data_valid_as_of_utc"], e["origin"] != "LEGACY_IMPORT",
                                e.get("source_file_id") or 0))
    comparison = compare(entries, RELATIONSHIP_FIELDS)
    return {
        "subject": {"child_lei": child_lei, "parent_lei": parent_lei,
                    "relationship_type": relationship_type},
        "interpretation": CONSOLIDATION_NOTE if relationship_type in CONSOLIDATION_TYPES else None,
        "entries": entries,
        "comparison": comparison,
        "assessment": assessment(comparison, entries),
        "absences": absences,
        "audit": audit,
    }


def _absences(cursor, tables, key):
    """
    Completed RR files that do not include the relationship, each with
    what its absence can mean (never a termination).
    """

    if "relationship_observations" not in tables:
        return []
    cursor.execute(
        "SELECT f.source_file_id, f.file_name, f.content_date_utc, f.publication_scope, "
        "b.batch_id, b.selection_json, "
        "b.quarantined_inserted + b.quarantined_already_recorded AS quarantined, "
        "EXISTS (SELECT 1 FROM relationship_batch_observations m JOIN relationship_observations o "
        "ON o.observation_id = m.observation_id WHERE m.batch_id = b.batch_id AND o.child_lei = %s "
        "AND o.parent_lei = %s AND o.relationship_type = %s) AS present "
        "FROM gleif_import_batches b JOIN gleif_source_files f ON f.source_file_id = b.source_file_id "
        "WHERE f.dataset = 'RR' AND b.status = 'COMPLETED' ORDER BY f.content_date_utc, b.batch_id", key)
    files = {}
    for row in cursor.fetchall():
        file = files.setdefault(row["source_file_id"], {"row": row, "present": False, "batches": []})
        file["present"] = file["present"] or bool(row["present"])
        file["batches"].append(row)
    absences = []
    for file in files.values():
        if file["present"]:
            continue
        meanings = []
        for row in file["batches"]:
            selection = json.loads(row["selection_json"])
            in_selection = selection["mode"] != "LEI_SET" or bool(
                {key[0], key[1]} & set(selection.get("leis", ())))
            meanings.append(absence_meaning(row["publication_scope"], selection, in_selection,
                                            int(row["quarantined"] or 0)))
        order = ("NOT_PUBLISHED_IN_FULL_DATASET", "ABSENCE_UNDETERMINED_QUARANTINE_PRESENT",
                 "ABSENT_FROM_DELTA", "ABSENT_SCOPE_UNKNOWN", "ABSENT_FROM_SAMPLE")
        row = file["row"]
        absences.append({
            "source": f"file {row['file_name']} (source file #{row['source_file_id']})",
            "data_valid_as_of_utc": _when(row["content_date_utc"]),
            "meaning": min(meanings, key=order.index),
            "note": "Absence from a file is not evidence that the relationship ended."})
    return absences


def parent_history(connection, child_lei):
    """
    Which parent relationships the published records report for a child,
    file by file (read-only). A parent missing from a later file is
    described with what that absence can mean, never as an end.
    """

    cursor = connection.cursor(dictionary=True)
    try:
        tables, _ = _schema(cursor)
        keys = set()
        if "relationship_observations" in tables:
            cursor.execute(
                "SELECT DISTINCT o.parent_lei, o.relationship_type FROM relationship_observations o "
                "JOIN relationship_batch_observations m ON m.observation_id = o.observation_id "
                "JOIN gleif_import_batches b ON b.batch_id = m.batch_id AND b.status = 'COMPLETED' "
                "WHERE o.child_lei = %s", (child_lei,))
            keys |= {(r["parent_lei"], r["relationship_type"]) for r in cursor.fetchall()}
        cursor.execute("SELECT DISTINCT parent_lei, relationship_type FROM relationship_evidence "
                       "WHERE child_lei = %s", (child_lei,))
        keys |= {(r["parent_lei"], r["relationship_type"]) for r in cursor.fetchall()}
    finally:
        cursor.close()
        connection.rollback()
    relationships = [relationship_history(connection, child_lei, parent, kind)
                     for parent, kind in sorted(keys)]
    return {
        "child_lei": child_lei,
        "relationships": [{
            "parent_lei": h["subject"]["parent_lei"],
            "relationship_type": h["subject"]["relationship_type"],
            "reported_in": [e["source"] for e in h["entries"]],
            "absent_from": h["absences"],
            "interpretation": h["interpretation"],
        } for h in relationships],
        "note": SOURCE_NOTE,
    }
