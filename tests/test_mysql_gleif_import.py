
"""
The GLEIF RR importer against the real test database
(corporate_intelligence_test only), with synthetic RR files. Every test
checks the stored rows and audit state, not only the outcome.
"""

import datetime
import os
import threading
import time

import pytest

from src.evidence_snapshot import as_version, snapshot_sha256
from src.gleif_import import (
    BatchBusy,
    ImportIntegrityError,
    LeaseLost,
    import_rr_file,
)
from src.gleif_observations import canonical_selection
from src.gleif_rr import SourceChangedError

from gleif_rr_samples import document, relationship, synthetic_lei, write
from test_mysql_evidence_snapshot import (  # noqa: F401
    block_default_connections,
    connect,
    pytestmark,
)
from test_mysql_report_registry import registry_db, seed, snapshot_now  # noqa: F401

PREFIX = "SYNTHGI"
A, B, C, D = (synthetic_lei(PREFIX + x) for x in "ABCD")
TYPE = "IS_DIRECTLY_CONSOLIDATED_BY"
ALL = canonical_selection("RR", "ALL_RECORDS")


class Crash(BaseException):
    """A simulated process death: nothing gets a chance to mark FAILED."""


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


def purge_m23b2b3_rows(cursor, files_where, params=()):
    """
    Remove rows of the m23b2b3 tables (when installed) for the files
    matching `files_where` (on gleif_source_files f), before their batches
    and files are removed. Requires @crie_gleif_maintenance = 1.
    """

    for table in ("gleif_batch_positions", "gleif_source_positions",
                  "gleif_batch_duplicate_occurrences", "gleif_batch_quarantined_records",
                  "gleif_quarantined_records"):
        cursor.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE "
                       "TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (table,))
        row = cursor.fetchone()                          # plain or dictionary cursor
        if not (list(row.values())[0] if isinstance(row, dict) else row[0]):
            continue
        if table == "gleif_batch_positions":
            cursor.execute("DELETE p FROM gleif_batch_positions p JOIN gleif_import_batches b "
                           "USING (batch_id) JOIN gleif_source_files f USING (source_file_id) "
                           f"WHERE {files_where}", params)
        else:
            cursor.execute(f"DELETE x FROM {table} x JOIN gleif_source_files f "
                           f"USING (source_file_id) WHERE {files_where}", params)


def cleanup():
    connection = connect()
    cursor = connection.cursor()
    try:
        like = PREFIX + "%"
        cursor.execute("SET @crie_gleif_maintenance = 1")
        cursor.execute("DELETE m FROM relationship_batch_observations m JOIN gleif_source_files f "
                       "USING (source_file_id) WHERE f.file_name LIKE 'synthetic-gi-%'")
        cursor.execute("DELETE p FROM relationship_observation_periods p JOIN "
                       "relationship_observations o USING (observation_id) WHERE o.child_lei LIKE %s",
                       (like,))
        cursor.execute("DELETE FROM relationship_observations WHERE child_lei LIKE %s", (like,))
        cursor.execute("DELETE FROM relationship_evidence WHERE child_lei LIKE %s", (like,))
        purge_m23b2b3_rows(cursor, "f.file_name LIKE 'synthetic-gi-%'")
        cursor.execute("DELETE b FROM gleif_import_batches b JOIN gleif_source_files f "
                       "USING (source_file_id) WHERE f.file_name LIKE 'synthetic-gi-%'")
        cursor.execute("DELETE FROM gleif_source_files WHERE file_name LIKE 'synthetic-gi-%'")
        cursor.execute("SET @crie_gleif_maintenance = 0")
        cursor.execute("DELETE FROM corporate_relationships WHERE child_lei LIKE %s", (like,))
        cursor.execute("DELETE FROM legal_entities WHERE lei LIKE %s", (like,))
        connection.commit()
    finally:
        cursor.close()
        connection.close()


@pytest.fixture
def db(tmp_path):
    installed = rows("SELECT COUNT(*) AS n FROM information_schema.TABLES WHERE "
                     "TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'relationship_batch_observations'")
    if not installed[0]["n"]:
        pytest.skip("GLEIF observations are not installed in the test database; apply "
                    "sql/test_schema_upgrade_m23b2b.sql (test database only).")
    cleanup()
    try:
        yield tmp_path
    finally:
        cleanup()


def trusted(counts):
    """The trusted-observation counters; these files never quarantine anything."""

    assert (counts["quarantined_inserted"], counts["quarantined_already_recorded"]) == (0, 0)
    return {k: v for k, v in counts.items() if not k.startswith("quarantined_")}


def anchors(summary):
    assert summary["anchors"]["NOT_CREATED_QUARANTINED"] == 0
    return {k: v for k, v in summary["anchors"].items() if k != "NOT_CREATED_QUARANTINED"}


def rr_file(directory, name, records, **header):
    return write(directory / f"synthetic-gi-{name}.xml", records, **header)


def run(path, selection=ALL, **kwargs):
    kwargs.setdefault("started_by", "synthetic test")
    return import_rr_file(connect, path, selection, **kwargs)


def batch_state(batch_id):
    """Everything a batch stands for, independent of row ids and timing."""

    (batch,) = rows("SELECT status, records_scanned, records_selected, observations_inserted, "
                    "observations_already_recorded, identical_duplicates FROM gleif_import_batches "
                    "WHERE batch_id = %s", (batch_id,))
    members = rows("SELECT o.source_record_ordinal AS first_ordinal, m.source_record_ordinal "
                   "AS ordinal, o.record_sha256, o.batch_id = m.batch_id AS created_here "
                   "FROM relationship_batch_observations m JOIN relationship_observations o "
                   "USING (observation_id) WHERE m.batch_id = %s ORDER BY m.source_record_ordinal",
                   (batch_id,))
    periods = rows("SELECT o.source_record_ordinal, p.period_index, p.period_type, p.start_text, "
                   "p.start_at_utc, p.end_text, p.end_at_utc FROM relationship_observation_periods p "
                   "JOIN relationship_observations o USING (observation_id) WHERE o.batch_id = %s "
                   "ORDER BY o.source_record_ordinal, p.period_index", (batch_id,))
    return batch, members, periods


def count(table, where="1", params=()):
    return rows(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params)[0]["n"]


def seven(**changes):
    """Seven distinct records (two relationships, varying statuses)."""

    statuses = ["ACTIVE", "INACTIVE"]
    return [relationship(A if i % 2 else C, B, status=statuses[i % 2],
                         reference=f"Synthetic reference {i}", **changes) for i in range(7)]


def test_complete_import_stores_exact_observations_and_audit_state(db):
    path = rr_file(db, "full", [
        relationship(A, B, validation="PARTIALLY_CORROBORATED", reference="Synthetic filing"),
        relationship(C, B, status="INACTIVE", registration="RETIRED", validation=None,
                     documents=None, periods=(), qualifiers=(), quantifiers=()),
    ])
    summary = run(path, chunk_size=1)

    assert (summary["status"], summary["acquired"], summary["coverage"]) == (
        "COMPLETED", "STARTED", "COMPLETE_PUBLISHED_DATASET")
    assert trusted(summary["counts"]) == {"records_scanned": 2, "records_selected": 2,
                                 "observations_inserted": 2, "observations_already_recorded": 0,
                                 "identical_duplicates": 0}
    assert summary["chunks_committed"] == 2
    observed = rows("SELECT * FROM relationship_observations WHERE batch_id = %s "
                    "ORDER BY source_record_ordinal", (summary["batch_id"],))
    first, second = observed
    assert (first["child_lei"], first["parent_lei"], first["relationship_type"]) == (A, B, TYPE)
    assert (first["validation_sources"], first["validation_reference"]) == (
        "PARTIALLY_CORROBORATED", "Synthetic filing")
    assert first["qualifiers"] and first["quantifiers"]
    assert first["last_update_at_utc"] == datetime.datetime(2026, 1, 15, 10, 0)
    # Unstated stays NULL; nothing is borrowed from elsewhere.
    for column in ("validation_sources", "validation_documents", "validation_reference",
                   "qualifiers", "quantifiers"):
        assert second[column] is None, column
    assert (second["relationship_status"], second["registration_status"]) == ("INACTIVE", "RETIRED")
    _, members, periods = batch_state(summary["batch_id"])
    assert [m["ordinal"] for m in members] == [1, 2] and all(m["created_here"] for m in members)
    assert [(p["period_type"], p["end_text"]) for p in periods] == [
        ("ACCOUNTING_PERIOD", "2023-12-31T00:00:00Z"), ("RELATIONSHIP_PERIOD", None)]
    # The data-valid-as-of time (ContentDate) is the file's; ingestion time is separate.
    (source,) = rows("SELECT content_date_utc, registered_at_utc, record_count, publication_scope "
                     "FROM gleif_source_files WHERE source_file_id = %s", (summary["source_file_id"],))
    assert source["content_date_utc"] == datetime.datetime(2026, 10, 22, 9, 0, 1, 250000)
    assert (source["record_count"], source["publication_scope"]) == (2, "FULL")
    # A new anchor where validation is stated; none invented where it is not.
    assert anchors(summary) == {"CREATED": 1, "EXISTING": 0, "NOT_CREATED_UNSTATED_VALIDATION": 1}
    (anchor,) = rows("SELECT snapshot_at_utc, first_batch_id, validation_sources FROM "
                     "relationship_evidence WHERE child_lei = %s", (A,))
    assert anchor == {"snapshot_at_utc": datetime.datetime(2026, 10, 22, 9, 0, 1),
                      "first_batch_id": summary["batch_id"],
                      "validation_sources": "PARTIALLY_CORROBORATED"}
    identities = rows("SELECT child_lei, relationship_status, registration_status, source_dataset "
                      "FROM corporate_relationships WHERE child_lei LIKE %s ORDER BY child_lei",
                      (PREFIX + "%",))
    assert [(i["relationship_status"], i["registration_status"]) for i in identities] == [
        ("ACTIVE", "PUBLISHED"), ("INACTIVE", "RETIRED")]
    assert rows("SELECT legal_name FROM legal_entities WHERE lei = %s", (A,)) == [{"legal_name": None}]


def test_identical_reimport_changes_nothing(db):
    path = rr_file(db, "same", seven())
    first = run(path, chunk_size=3)
    before = {t: count(t) for t in ("gleif_source_files", "gleif_import_batches",
                                    "relationship_observations", "relationship_batch_observations",
                                    "relationship_observation_periods", "relationship_evidence")}

    again = run(path, chunk_size=3)
    renamed = path.with_name("synthetic-gi-renamed.xml")
    renamed.write_bytes(path.read_bytes())
    third = run(renamed)

    for result in (again, third):
        assert (result["acquired"], result["status"], result["batch_id"]) == (
            "ALREADY_COMPLETED", "COMPLETED", first["batch_id"])
        assert result["source_file_registered"] is False and result["chunks_committed"] == 0
    assert {t: count(t) for t in before} == before
    # The file keeps the name it was first registered with.
    assert rows("SELECT file_name FROM gleif_source_files WHERE source_file_id = %s",
                (first["source_file_id"],)) == [{"file_name": "synthetic-gi-same.xml"}]


def test_changed_content_under_the_same_filename_is_a_new_file(db):
    path = rr_file(db, "name", [relationship(A, B)])
    first = run(path)
    rr_file(db, "name", [relationship(A, B, status="INACTIVE")])
    second = run(path)

    assert second["source_file_id"] != first["source_file_id"]
    assert second["batch_id"] != first["batch_id"] and second["status"] == "COMPLETED"
    statuses = [r["relationship_status"] for r in rows(
        "SELECT relationship_status FROM relationship_observations WHERE child_lei = %s "
        "ORDER BY observation_id", (A,))]
    assert statuses == ["ACTIVE", "INACTIVE"]


def test_two_files_with_the_same_content_date_keep_both_and_the_first_anchor(db):
    one = run(rr_file(db, "date-1", [relationship(A, B, validation="ENTITY_SUPPLIED_ONLY")]))
    two = run(rr_file(db, "date-2", [relationship(A, B, validation="FULLY_CORROBORATED"),
                                     relationship(C, B)]))

    assert one["source_file_id"] != two["source_file_id"]
    assert anchors(two) == {"CREATED": 1, "EXISTING": 1, "NOT_CREATED_UNSTATED_VALIDATION": 0}
    observed = [r["validation_sources"] for r in rows(
        "SELECT validation_sources FROM relationship_observations WHERE child_lei = %s "
        "ORDER BY observation_id", (A,))]
    assert observed == ["ENTITY_SUPPLIED_ONLY", "FULLY_CORROBORATED"]
    (anchor,) = rows("SELECT first_batch_id, validation_sources FROM relationship_evidence "
                     "WHERE child_lei = %s", (A,))
    assert anchor == {"first_batch_id": one["batch_id"], "validation_sources": "ENTITY_SUPPLIED_ONLY"}


def test_repeated_records_for_one_relationship_within_a_file(db):
    summary = run(rr_file(db, "repeats", [relationship(A, B), relationship(A, B),
                                          relationship(A, B, status="INACTIVE")]), chunk_size=2)

    assert trusted(summary["counts"]) == {"records_scanned": 3, "records_selected": 3,
                                 "observations_inserted": 2, "observations_already_recorded": 0,
                                 "identical_duplicates": 1}
    _, members, _ = batch_state(summary["batch_id"])
    assert [m["ordinal"] for m in members] == [1, 3]       # the conflicting record is kept


def test_overlapping_selections_keep_complete_membership(db):
    # 1 A->B   2 C->B   3 A->B inactive   4 repeat of 2   5 D->A
    path = rr_file(db, "overlap", [relationship(A, B), relationship(C, B),
                                   relationship(A, B, status="INACTIVE"), relationship(C, B),
                                   relationship(D, A)])
    first_n = run(path, canonical_selection("RR", "FIRST_N", limit=2), chunk_size=1)
    lei_set = run(path, canonical_selection("RR", "LEI_SET", leis=[A], match="CHILD_OR_PARENT"),
                  chunk_size=2)
    whole = run(path, chunk_size=2)

    member_ordinals = {name: [m["ordinal"] for m in batch_state(s["batch_id"])[1]]
                       for name, s in (("first", first_n), ("lei", lei_set), ("all", whole))}
    assert member_ordinals == {"first": [1, 2], "lei": [1, 3, 5], "all": [1, 2, 3, 5]}
    assert first_n["counts"]["records_scanned"] == 2            # never read past its sample
    assert trusted(lei_set["counts"]) == {"records_scanned": 5, "records_selected": 3,
                                 "observations_inserted": 2, "observations_already_recorded": 1,
                                 "identical_duplicates": 0}
    assert lei_set["matched_endpoints"] == {"CHILD": 2, "PARENT": 1, "BOTH": 0}
    assert trusted(whole["counts"]) == {"records_scanned": 5, "records_selected": 5,
                               "observations_inserted": 0, "observations_already_recorded": 4,
                               "identical_duplicates": 1}
    assert (first_n["coverage"], lei_set["coverage"], whole["coverage"]) == (
        "COMPLETE_SAMPLE_OF_FILE", "COMPLETE_SAMPLE_OF_FILE", "COMPLETE_PUBLISHED_DATASET")
    creators = {r["source_record_ordinal"]: r["batch_id"] for r in rows(
        "SELECT source_record_ordinal, batch_id FROM relationship_observations "
        "WHERE source_file_id = %s", (whole["source_file_id"],))}
    assert creators == {1: first_n["batch_id"], 2: first_n["batch_id"],
                        3: lei_set["batch_id"], 5: lei_set["batch_id"]}


def equivalent(reference_batch, resumed_batch):
    """Same final observations, membership, periods and counters."""

    one, two = batch_state(reference_batch), batch_state(resumed_batch)
    strip = lambda state: (state[0], [{k: v for k, v in m.items()} for m in state[1]],  # noqa: E731
                           state[2])
    return strip(one) == strip(two)


def test_crash_after_committed_chunks_resumes_to_the_same_result(db):
    reference = run(rr_file(db, "ref", seven(), content_date="2026-10-01T00:00:00Z"), chunk_size=2)
    path = rr_file(db, "crash", seven(), content_date="2026-10-02T00:00:00Z")

    def die(chunk):
        if chunk == 2:
            raise Crash()

    with pytest.raises(Crash):
        run(path, chunk_size=2, lease_seconds=1, hooks={"after_commit": die})
    (stuck,) = rows("SELECT b.batch_id, b.status, b.records_scanned, b.attempt FROM "
                    "gleif_import_batches b JOIN gleif_source_files f USING (source_file_id) "
                    "WHERE f.file_name = 'synthetic-gi-crash.xml'")
    assert (stuck["status"], stuck["records_scanned"], stuck["attempt"]) == ("STARTED", 4, 1)
    assert count("relationship_batch_observations", "batch_id = %s", (stuck["batch_id"],)) == 4

    time.sleep(1.5)                                         # let the dead worker's lease expire
    resumed = run(path, chunk_size=2)
    assert (resumed["acquired"], resumed["attempt"], resumed["status"]) == ("TAKEN_OVER", 2, "COMPLETED")
    assert resumed["chunks_committed"] == 2                 # only records 5..7 were redone
    assert equivalent(reference["batch_id"], resumed["batch_id"])


def test_injected_failure_rolls_back_the_chunk_and_resume_matches(db):
    reference = run(rr_file(db, "ref2", seven(), content_date="2026-10-01T00:00:00Z"), chunk_size=3)
    path = rr_file(db, "fail", seven(), content_date="2026-10-03T00:00:00Z")

    def fail(chunk):
        if chunk == 2:
            raise RuntimeError("Synthetic failure inside chunk 2")

    with pytest.raises(RuntimeError, match="Synthetic failure"):
        run(path, chunk_size=3, hooks={"before_commit": fail})
    (failed,) = rows("SELECT b.batch_id, b.status, b.records_scanned, b.failure_reason, "
                     "f.publication_scope FROM gleif_import_batches b JOIN gleif_source_files f "
                     "USING (source_file_id) WHERE f.file_name = 'synthetic-gi-fail.xml'")
    assert (failed["status"], failed["records_scanned"]) == ("FAILED", 3)
    assert "Synthetic failure inside chunk 2" in failed["failure_reason"]
    # Nothing of chunk 2 survived, and the failed batch is not presented as complete.
    assert [m["ordinal"] for m in batch_state(failed["batch_id"])[1]] == [1, 2, 3]
    from src.gleif_observations import batch_coverage
    assert batch_coverage(failed["status"], "ALL_RECORDS", "FULL") == "INCOMPLETE_IMPORT"

    resumed = run(path, chunk_size=3)
    assert (resumed["acquired"], resumed["status"]) == ("RESUMED", "COMPLETED")
    assert equivalent(reference["batch_id"], resumed["batch_id"])


def test_live_lease_blocks_and_stale_worker_is_fenced_after_takeover(db):
    path = rr_file(db, "stale", seven())
    reference = run(rr_file(db, "ref3", seven(), content_date="2026-10-01T00:00:00Z"), chunk_size=2)
    events = []

    def pause(chunk):
        if chunk != 1:
            return
        # Worker A still holds a live lease: B is refused.
        with pytest.raises(BatchBusy, match="live lease"):
            run(path, chunk_size=2)
        time.sleep(1.5)                                     # A stalls past its lease
        events.append(run(path, chunk_size=2))              # B takes over and finishes

    with pytest.raises(LeaseLost):
        run(path, chunk_size=2, lease_seconds=1, hooks={"after_commit": pause})

    (taken,) = events
    assert (taken["acquired"], taken["status"], taken["attempt"]) == ("TAKEN_OVER", "COMPLETED", 2)
    # A's later chunk wrote nothing and could not fail B's completed batch.
    assert equivalent(reference["batch_id"], taken["batch_id"])
    assert rows("SELECT status, failure_reason FROM gleif_import_batches WHERE batch_id = %s",
                (taken["batch_id"],)) == [{"status": "COMPLETED", "failure_reason": None}]


def test_concurrent_acquisition_has_one_winner(db):
    path = rr_file(db, "race", seven())
    barrier, outcomes = threading.Barrier(2), []

    def attempt():
        def wait(_):
            pass
        try:
            barrier.wait()
            outcomes.append(run(path, chunk_size=1, hooks={"after_commit": wait}))
        except BatchBusy as error:
            outcomes.append(error)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    started = [o for o in outcomes if isinstance(o, dict) and o["acquired"] == "STARTED"]
    others = [o for o in outcomes if not (isinstance(o, dict) and o["acquired"] == "STARTED")]
    assert len(started) == 1 and len(others) == 1
    assert isinstance(others[0], BatchBusy) or others[0]["acquired"] == "ALREADY_COMPLETED"
    assert count("gleif_import_batches b JOIN gleif_source_files f USING (source_file_id)",
                 "f.file_name = 'synthetic-gi-race.xml'") == 1
    assert started[0]["counts"]["observations_inserted"] == 7


def test_same_position_with_different_content_is_an_integrity_error(db):
    path = rr_file(db, "conflict", [relationship(A, B), relationship(C, B)])
    sample = run(path, canonical_selection("RR", "FIRST_N", limit=1))

    # Simulate stored evidence that disagrees with the file at record 2
    # (as if parsed differently): a raw row under a leased batch.
    from src.gleif_observations import canonical_bytes, selection_fingerprint
    import uuid
    connection = connect()
    cursor = connection.cursor()
    lease = uuid.uuid4().hex
    selection = canonical_selection("RR", "FIRST_N", limit=2)
    try:
        cursor.execute("SET @crie_gleif_lease = %s", (lease,))
        cursor.execute(
            "INSERT INTO gleif_import_batches (source_file_id, selection_mode, selection_limit, "
            "selection_json, selection_fingerprint, selection_description, parser_version, "
            "record_hash_version, lease_token, lease_expires_at_utc, started_by) VALUES "
            "(%s, 'FIRST_N', 2, %s, %s, 'Synthetic conflicting row', 'other', 1, %s, "
            "UTC_TIMESTAMP(6) + INTERVAL 1 MINUTE, 'synthetic test')",
            (sample["source_file_id"], canonical_bytes(selection).decode(),
             selection_fingerprint(selection), lease))
        other = cursor.lastrowid
        # Record 2 of this file is C->B; store something else at that position.
        cursor.execute("INSERT INTO relationship_observations (source_file_id, batch_id, "
                       "source_record_ordinal, record_hash_version, record_sha256, child_lei, "
                       "parent_lei, relationship_type) VALUES (%s, %s, 2, 1, %s, %s, %s, %s)",
                       (sample["source_file_id"], other, "e" * 64, A, B, TYPE))
        connection.commit()
    finally:
        connection.rollback()
        cursor.execute("SET @crie_gleif_lease = NULL")
        cursor.close()
        connection.close()

    with pytest.raises(ImportIntegrityError, match="Record 2 .* different content"):
        run(path)
    (batch,) = rows("SELECT b.batch_id, b.status, b.records_scanned, b.failure_reason FROM "
                    "gleif_import_batches b WHERE b.source_file_id = %s AND b.selection_mode = "
                    "'ALL_RECORDS'", (sample["source_file_id"],))
    assert (batch["status"], batch["records_scanned"]) == ("FAILED", 0)
    assert "ImportIntegrityError" in batch["failure_reason"]
    assert count("relationship_batch_observations", "batch_id = %s", (batch["batch_id"],)) == 0


@pytest.mark.parametrize("keep_identity", [False, True])
def test_file_changing_during_import_never_completes(db, keep_identity, monkeypatch):
    import src.gleif_rr
    monkeypatch.setattr(src.gleif_rr, "BLOCK_BYTES", 512)   # read the file a little at a time
    path = rr_file(db, f"moving-{keep_identity}", seven())

    def tamper(chunk):
        if chunk != 1:
            return
        stat = os.stat(path)
        text = path.read_text()
        path.write_text(text.replace("Synthetic reference 6", "Synthetic reference X"))
        if keep_identity:     # same size and mtime: only the hash can tell
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    with pytest.raises(SourceChangedError):
        run(path, chunk_size=2, hooks={"after_commit": tamper})
    (batch,) = rows("SELECT b.batch_id, b.status, b.records_scanned, b.failure_reason FROM "
                    "gleif_import_batches b JOIN gleif_source_files f USING (source_file_id) "
                    "WHERE f.file_name = %s", (path.name,))
    assert batch["status"] == "FAILED" and "SourceChangedError" in batch["failure_reason"]
    # The audit trail of what was committed stays; it is not a completed import.
    assert count("relationship_batch_observations", "batch_id = %s", (batch["batch_id"],)) \
        == batch["records_scanned"] > 0
    if not keep_identity:
        assert batch["records_scanned"] == 2                # stopped at the next chunk
    else:
        assert batch["records_scanned"] == 6                # record 7 refused before writing
        assert "Record 7 differs" in batch["failure_reason"]
    # No changed content was ever stored under the registered file.
    assert count("relationship_observations", "validation_reference = 'Synthetic reference X'") == 0


def test_header_count_mismatch_writes_nothing(db):
    path = rr_file(db, "count", [relationship(A, B)], record_count=3)
    with pytest.raises(ValueError, match="RecordCount"):
        run(path)
    assert count("gleif_source_files", "file_name = 'synthetic-gi-count.xml'") == 0


def test_imports_do_not_change_snapshot_versions_1_to_3(db, registry_db):
    _, fixtures, _ = registry_db
    case_id, *_ = seed(fixtures)
    before = snapshot_now(case_id)

    run(rr_file(db, "snap", seven()))
    after = snapshot_now(case_id)

    for version in ("1", "2", "3"):
        assert snapshot_sha256(as_version(before, version)) == snapshot_sha256(as_version(after, version))
