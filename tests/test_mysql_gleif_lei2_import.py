
"""
Stage 3b: LEI2 ingestion and quarantine against the real test database
(corporate_intelligence_test only), with synthetic GLEIF files. Every test
checks stored rows and audit state.
"""

import json
import os
import threading
import time

import pytest

from src.evidence_snapshot import as_version, snapshot_sha256
from src.gleif_import import BatchBusy, import_lei2_file, import_rr_file
from src.gleif_observations import canonical_selection, record_sha256
from src.gleif_rr import SourceChangedError

from gleif_lei2_samples import event, record, write as write_lei2
from gleif_rr_samples import LOU, relationship, synthetic_lei, write as write_rr
from test_mysql_evidence_snapshot import block_default_connections, connect, pytestmark  # noqa: F401
from test_mysql_gleif_import import purge_m23b2b3_rows
from test_mysql_report_registry import registry_db, seed, snapshot_now  # noqa: F401

PREFIX = "SYNTHL3"
A, B, C = (synthetic_lei(PREFIX + x) for x in "ABC")
BAD = A[:-2] + f"{(int(A[-2:]) + 1) % 100:02d}"            # LEI form, wrong check digits
ALL = canonical_selection("LEI2", "ALL_RECORDS")


class Crash(BaseException):
    """A simulated process death."""


def rows(sql, params=()):
    connection = connect()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(sql, params)
        return cursor.fetchall()
    finally:
        cursor.close()
        connection.rollback()
        connection.close()


def count(table, where="1", params=()):
    return rows(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params)[0]["n"]


def cleanup():
    connection = connect()
    cursor = connection.cursor()
    try:
        like, files = PREFIX + "%", "f.file_name LIKE 'synthetic-l3-%'"
        cursor.execute("SET @crie_gleif_maintenance = 1")
        purge_m23b2b3_rows(cursor, files)
        for table in ("relationship_batch_observations", "entity_batch_observations"):
            cursor.execute(f"DELETE m FROM {table} m JOIN gleif_source_files f "
                           f"USING (source_file_id) WHERE {files}")
        cursor.execute("DELETE p FROM relationship_observation_periods p JOIN "
                       "relationship_observations o USING (observation_id) JOIN gleif_source_files f "
                       f"USING (source_file_id) WHERE {files}")
        for table in ("relationship_observations", "legal_entity_observations"):
            cursor.execute(f"DELETE o FROM {table} o JOIN gleif_source_files f "
                           f"USING (source_file_id) WHERE {files}")
        cursor.execute("DELETE FROM relationship_evidence WHERE child_lei LIKE %s", (like,))
        cursor.execute("DELETE b FROM gleif_import_batches b JOIN gleif_source_files f "
                       f"USING (source_file_id) WHERE {files}")
        cursor.execute("DELETE FROM gleif_source_files WHERE file_name LIKE 'synthetic-l3-%'")
        cursor.execute("SET @crie_gleif_maintenance = 0")
        cursor.execute("DELETE FROM corporate_relationships WHERE child_lei LIKE %s "
                       "OR parent_lei LIKE %s", (like, like))
        cursor.execute("DELETE FROM legal_entities WHERE lei LIKE %s", (like,))
        connection.commit()
    finally:
        cursor.close()
        connection.close()


@pytest.fixture
def db(tmp_path):
    if not count("information_schema.TABLES", "TABLE_SCHEMA = DATABASE() AND TABLE_NAME = "
                 "'gleif_batch_positions'"):
        pytest.skip("m23b2b3 is not installed in the test database")
    cleanup()
    try:
        yield tmp_path
    finally:
        cleanup()


def lei2(directory, name, records, **kwargs):
    return write_lei2(directory / f"synthetic-l3-{name}.xml", records, **kwargs)


def rr(directory, name, records, **kwargs):
    return write_rr(directory / f"synthetic-l3-{name}.xml", records, **kwargs)


def run(path, selection=ALL, **kwargs):
    kwargs.setdefault("started_by", "synthetic test")
    return import_lei2_file(connect, path, selection, **kwargs)


def entity(lei, **kwargs):
    return record(lei=lei, **kwargs)


def test_lei2_import_stores_canonical_records_typed_fields_and_identity(db):
    sources = [("2026-10-22T08:00:00Z", LOU, 2)]
    path = lei2(db, "full", [
        entity(A, other_names=(("Synthetic Alte AG", "PREVIOUS_LEGAL_NAME", "de"),),
               category="GENERAL", events=(event(),)),
        entity(B, status="NULL", category=None, legal_form=None, creation=None)],
        sources=sources)
    summary = run(path, chunk_size=1)

    assert (summary["status"], summary["coverage"]) == ("COMPLETED", "COMPLETE_PUBLISHED_DATASET")
    assert summary["coverage_properties"]["trusted_complete_dataset"] is True
    assert summary["counts"]["observations_inserted"] == 2 and summary["chunks_committed"] == 2
    observed = rows("SELECT * FROM legal_entity_observations WHERE batch_id = %s "
                    "ORDER BY source_record_ordinal", (summary["batch_id"],))
    first, second = observed
    # The stored canonical record reproduces the record hash exactly.
    for row in observed:
        assert record_sha256(json.loads(row["canonical_record"])) == row["record_sha256"]
    assert (first["legal_name"], first["legal_name_language"], first["entity_category"]) == (
        "Synthetic Holdings AG", "de", "GENERAL")
    assert json.loads(first["other_names"])[0]["type"] == "PREVIOUS_LEGAL_NAME"
    assert json.loads(first["canonical_record"])["legal_entity_events"][0]["type"] == "CHANGE_LEGAL_NAME"
    assert (second["entity_status"], second["entity_category"], second["legal_form_code"]) == (
        "NULL", None, None)                                    # stated NULL vs absent
    assert {r["identifier_check"] for r in observed} == {"VALID"}
    # Identity rows with first-seen values; header metadata at file level.
    assert rows("SELECT legal_name FROM legal_entities WHERE lei = %s", (A,)) == [
        {"legal_name": "Synthetic Holdings AG"}]
    (source,) = rows("SELECT dataset, originator, header_extension, header_source_counts_consistent "
                     "FROM gleif_source_files WHERE source_file_id = %s", (summary["source_file_id"],))
    assert source["dataset"] == "LEI2" and source["originator"] is None
    assert json.loads(source["header_extension"])["sources"][0]["record_count"] == 2
    assert source["header_source_counts_consistent"] == 1


def test_later_files_never_overwrite_identity_or_observations(db):
    first = run(lei2(db, "jan", [entity(A, name="Synthetic Holdings AG")],
                     content_date="2026-01-01T00:00:00Z"))
    second = run(lei2(db, "feb", [entity(A, name="Synthetic Holdings SE")],
                      content_date="2026-02-01T00:00:00Z"))

    assert rows("SELECT legal_name FROM legal_entities WHERE lei = %s", (A,)) == [
        {"legal_name": "Synthetic Holdings AG"}]
    names = [r["legal_name"] for r in rows(
        "SELECT legal_name FROM legal_entity_observations WHERE lei = %s ORDER BY observation_id", (A,))]
    assert names == ["Synthetic Holdings AG", "Synthetic Holdings SE"]
    assert first["batch_id"] != second["batch_id"]


def test_identical_reimport_changes_nothing(db):
    path = lei2(db, "same", [entity(A), entity(B)])
    first = run(path)
    before = {t: count(t) for t in ("legal_entity_observations", "entity_batch_observations",
                                    "gleif_batch_positions", "legal_entities")}
    again = run(path)
    assert (again["acquired"], again["batch_id"]) == ("ALREADY_COMPLETED", first["batch_id"])
    assert {t: count(t) for t in before} == before


def test_invalid_primary_lei_is_quarantined_without_trusted_identity(db):
    path = lei2(db, "quarantine", [entity(A), entity(BAD, name="Synthetic Annulled AG"),
                                   entity(BAD, name="Synthetic Annulled AG")])
    summary = run(path)

    counts = summary["counts"]
    assert (counts["observations_inserted"], counts["quarantined_inserted"],
            counts["identical_duplicates"]) == (1, 1, 1)
    assert summary["coverage"] == "COMPLETE_FILE_SCAN_WITH_QUARANTINED_RECORDS"
    assert not summary["coverage_properties"]["trusted_complete_dataset"]
    (held,) = rows("SELECT dataset, subject_key, source_record_ordinal, canonical_record, identifiers, "
                   "reason_flags, record_sha256 FROM gleif_quarantined_records WHERE batch_id = %s",
                   (summary["batch_id"],))
    assert (held["dataset"], held["subject_key"], held["source_record_ordinal"]) == ("LEI2", BAD, 2)
    assert json.loads(held["identifiers"]) == [
        {"role": "LEI", "value": BAD, "issue": "IDENTIFIER_FAILS_ISO_17442_CHECK_DIGITS"}]
    assert record_sha256(json.loads(held["canonical_record"])) == held["record_sha256"]
    # The repeat is a duplicate position of the quarantined record.
    assert rows("SELECT source_record_ordinal, repeats_ordinal FROM gleif_batch_duplicate_occurrences "
                "WHERE batch_id = %s", (summary["batch_id"],)) == [
        {"source_record_ordinal": 3, "repeats_ordinal": 2}]
    assert count("legal_entities", "lei = %s", (BAD,)) == 0
    assert count("legal_entity_observations", "lei = %s", (BAD,)) == 0


def test_invalid_secondary_identifiers_stay_flags(db):
    summary = run(lei2(db, "secondary", [entity(A, successors=(BAD,))]))

    (observed,) = rows("SELECT identifier_check, identifier_flags, successor_lei FROM "
                       "legal_entity_observations WHERE batch_id = %s", (summary["batch_id"],))
    assert observed["identifier_check"] == "SECONDARY_IDENTIFIER_FLAGGED"
    assert json.loads(observed["identifier_flags"]) == [
        {"role": "SuccessorEntity[0]", "value": BAD, "issue": "IDENTIFIER_FAILS_ISO_17442_CHECK_DIGITS"}]
    assert summary["counts"]["quarantined_inserted"] == 0
    assert count("legal_entities", "lei = %s", (BAD,)) == 0          # never promoted


def test_rr_untrusted_endpoints_are_quarantined_and_lou_flags_kept(db):
    path = rr(db, "rr", [relationship(A, B), relationship(BAD, B),
                         relationship(C, B, child_type="ISO_17442_COMPATIBLE")])
    text = path.read_text()
    flagged_lou = BAD
    path.write_text(text.replace(f"<rr:ManagingLOU>{LOU}</rr:ManagingLOU>",
                                 f"<rr:ManagingLOU>{flagged_lou}</rr:ManagingLOU>", 1))
    summary = import_rr_file(connect, path, canonical_selection("RR", "ALL_RECORDS"),
                             started_by="synthetic test")

    assert (summary["counts"]["observations_inserted"], summary["counts"]["quarantined_inserted"]) == (1, 2)
    assert summary["anchors"]["NOT_CREATED_QUARANTINED"] == 2
    issues = sorted(json.loads(r["identifiers"])[0]["issue"] for r in rows(
        "SELECT identifiers FROM gleif_quarantined_records WHERE batch_id = %s", (summary["batch_id"],)))
    assert issues == ["IDENTIFIER_FAILS_ISO_17442_CHECK_DIGITS", "PROVISIONAL_NODE_IDENTIFIER"]
    for lei in (BAD, C):                                     # nothing trusted from quarantine
        assert count("legal_entities", "lei = %s", (lei,)) == 0
        assert count("corporate_relationships", "child_lei = %s", (lei,)) == 0
        assert count("relationship_evidence", "child_lei = %s", (lei,)) == 0
    (observed,) = rows("SELECT identifier_flags FROM relationship_observations WHERE batch_id = %s",
                       (summary["batch_id"],))
    assert json.loads(observed["identifier_flags"])[0]["role"] == "ManagingLOU"


def test_conflicting_records_for_one_lei_are_both_kept(db):
    summary = run(lei2(db, "conflict", [entity(A, status="ACTIVE"), entity(A, status="INACTIVE")]))
    assert summary["counts"]["observations_inserted"] == 2
    assert [r["entity_status"] for r in rows(
        "SELECT entity_status FROM legal_entity_observations WHERE lei = %s ORDER BY source_record_ordinal",
        (A,))] == ["ACTIVE", "INACTIVE"]


def test_overlapping_selections_share_observations_and_quarantine(db):
    path = lei2(db, "overlap", [entity(BAD), entity(A), entity(B)])
    first_n = run(path, canonical_selection("LEI2", "FIRST_N", limit=2))
    lei_set = run(path, canonical_selection("LEI2", "LEI_SET", leis=[B], match="ENTITY"))
    whole = run(path)

    assert first_n["coverage"] == "COMPLETE_SAMPLE_OF_FILE"
    assert lei_set["matched_endpoints"] == {"ENTITY": 1}
    counts = whole["counts"]
    assert (counts["observations_already_recorded"], counts["quarantined_already_recorded"],
            counts["observations_inserted"], counts["quarantined_inserted"]) == (2, 1, 0, 0)
    assert count("gleif_quarantined_records", "source_file_id = %s", (whole["source_file_id"],)) == 1


def test_interrupted_import_resumes_to_the_same_result(db):
    records = [entity(synthetic_lei(f"{PREFIX}R{i}"), name=f"Synthetic {i}") for i in range(5)]
    reference = run(lei2(db, "ref", records + [entity(BAD)], content_date="2026-10-01T00:00:00Z"),
                    chunk_size=2)
    path = lei2(db, "crash", records + [entity(BAD)], content_date="2026-10-02T00:00:00Z")

    def die(chunk):
        if chunk == 2:
            raise Crash()

    with pytest.raises(Crash):
        run(path, chunk_size=2, lease_seconds=1, hooks={"after_commit": die})
    time.sleep(1.5)
    resumed = run(path, chunk_size=2)
    assert (resumed["acquired"], resumed["status"]) == ("TAKEN_OVER", "COMPLETED")
    assert resumed["counts"] == reference["counts"]


def test_source_change_during_import_fails_the_batch(db, monkeypatch):
    import src.gleif_lei2
    monkeypatch.setattr(src.gleif_lei2, "BLOCK_BYTES", 512)
    path = lei2(db, "moving", [entity(synthetic_lei(f"{PREFIX}M{i}"), name=f"Name {i}")
                               for i in range(5)])

    def tamper(chunk):
        if chunk == 1:
            stat = os.stat(path)
            path.write_text(path.read_text().replace("Name 4", "Name X"))
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    with pytest.raises(SourceChangedError):
        run(path, chunk_size=2, hooks={"after_commit": tamper})
    (batch,) = rows("SELECT b.status FROM gleif_import_batches b JOIN gleif_source_files f "
                    "USING (source_file_id) WHERE f.file_name = %s", (path.name,))
    assert batch["status"] == "FAILED"
    assert count("legal_entity_observations", "legal_name = 'Name X'") == 0


def test_concurrent_imports_of_one_batch_have_one_winner(db):
    path = lei2(db, "race", [entity(synthetic_lei(f"{PREFIX}C{i}")) for i in range(4)])
    barrier, outcomes = threading.Barrier(2), []

    def attempt():
        try:
            barrier.wait()
            outcomes.append(run(path, chunk_size=1)["acquired"])
        except BatchBusy:
            outcomes.append("BUSY")

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert sorted(outcomes) in (["BUSY", "STARTED"], ["ALREADY_COMPLETED", "STARTED"])
    assert count("legal_entity_observations o JOIN gleif_source_files f USING (source_file_id)",
                 "f.file_name = %s", (path.name,)) == 4


def test_lei2_imports_do_not_change_snapshot_versions_1_to_3(db, registry_db):
    _, fixtures, _ = registry_db
    case_id, *_ = seed(fixtures)
    before = snapshot_now(case_id)
    run(lei2(db, "snap", [entity(A), entity(BAD)]))
    after = snapshot_now(case_id)
    for version in ("1", "2", "3"):
        assert snapshot_sha256(as_version(before, version)) == snapshot_sha256(as_version(after, version))
