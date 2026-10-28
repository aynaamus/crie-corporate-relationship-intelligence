
"""
Quarantine, duplicate occurrences and the replacement batch rules against
the real test database (corporate_intelligence_test only), with synthetic
rows. Skipped until sql/test_schema_upgrade_m23b2b3.sql is installed.
"""

import json
import time
import uuid

import pytest

from src.gleif_observations import canonical_selection, coverage_properties, record_sha256

from test_mysql_gleif_observations import (  # noqa: F401
    CHILD,
    KEY,
    PARENT,
    block_default_connections,
    connect,
    expect,
    gleif,
    pytestmark,
    record,
)

BAD_CHILD = CHILD[:-2] + f"{(int(CHILD[-2:]) + 1) % 100:02d}"      # LEI form, wrong check digits
SUBJECT = f"{BAD_CHILD}|{PARENT}|{KEY[2]}"


def untrusted(status="ACTIVE"):
    return {**record(status), "child_lei": BAD_CHILD}


@pytest.fixture
def quarantine(gleif):
    if not gleif.installed("gleif_quarantined_records"):
        pytest.skip("m23b2b3 is not installed in the test database; apply "
                    "sql/test_schema_upgrade_m23b2b3.sql (test database only).")
    return Quarantine(gleif)


class Quarantine:
    def __init__(self, gleif):
        self.gleif = gleif

    def add(self, batch_id, file_id, ordinal, lease, fields=None, link=True, subject=SUBJECT):
        fields = fields or untrusted()
        quarantine_id, _ = self.gleif.run(
            "INSERT INTO gleif_quarantined_records (dataset, source_file_id, batch_id, "
            "source_record_ordinal, record_hash_version, record_sha256, subject_key, "
            "canonical_record, identifiers, reason_flags) VALUES ('RR', %s, %s, %s, 1, %s, %s, %s, %s, %s)",
            (file_id, batch_id, ordinal, record_sha256(fields), subject, json.dumps(fields),
             json.dumps([{"role": "StartNode", "value": fields["child_lei"], "node_id_type": "LEI",
                          "issue": "IDENTIFIER_FAILS_ISO_17442_CHECK_DIGITS"}]),
             json.dumps(["IDENTIFIER_FAILS_ISO_17442_CHECK_DIGITS"])), lease=lease)
        if link:
            self.link(batch_id, file_id, quarantine_id, ordinal, lease)
        return quarantine_id

    def link(self, batch_id, file_id, quarantine_id, ordinal, lease):
        self.gleif.run("INSERT INTO gleif_batch_quarantined_records (batch_id, quarantine_id, "
                       "source_file_id, source_record_ordinal) VALUES (%s, %s, %s, %s)",
                       (batch_id, quarantine_id, file_id, ordinal), lease=lease)

    def progress(self, batch_id, lease, scanned, selected, inserted=0, already=0,
                 q_inserted=0, q_already=0, duplicates=0):
        return self.gleif.run(
            "UPDATE gleif_import_batches SET records_scanned = %s, records_selected = %s, "
            "observations_inserted = %s, observations_already_recorded = %s, "
            "quarantined_inserted = %s, quarantined_already_recorded = %s, "
            "identical_duplicates = %s, progress_at_utc = UTC_TIMESTAMP(6), "
            "lease_expires_at_utc = UTC_TIMESTAMP(6) + INTERVAL 10 MINUTE WHERE batch_id = %s",
            (scanned, selected, inserted, already, q_inserted, q_already, duplicates, batch_id),
            lease=lease)[1]


def counts(gleif, batch_id):
    (row,) = gleif.rows("SELECT status, records_scanned, records_selected, observations_inserted, "
                        "observations_already_recorded, quarantined_inserted, "
                        "quarantined_already_recorded, identical_duplicates FROM "
                        "gleif_import_batches WHERE batch_id = %s", (batch_id,))
    return row


def test_all_records_with_quarantine_and_a_repeat_is_accounted_exactly(gleif, quarantine):
    # Counting a duplicate whose occurrence was never stored cannot complete.
    unstored = gleif.source_file("no occurrence", records=2)
    batch_x, lease_x = gleif.start(unstored)
    quarantine.add(batch_x, unstored, 1, lease_x)
    quarantine.progress(batch_x, lease_x, 2, 2, q_inserted=1, duplicates=1)
    expect(1644, gleif.complete, batch_x, lease_x)

    file_id = gleif.source_file(records=3)
    batch_id, lease = gleif.start(file_id)
    gleif.observe(batch_id, file_id, 1, lease)
    quarantine.add(batch_id, file_id, 2, lease)
    # Record 3 repeats record 2 exactly: not stored again, but its position is.
    expect(1062, quarantine.add, batch_id, file_id, 3, lease, link=False)
    gleif.duplicate(batch_id, file_id, 3, 2, lease, record_sha256(untrusted()))
    quarantine.progress(batch_id, lease, 3, 3, inserted=1, q_inserted=1, duplicates=1)
    gleif.complete(batch_id, lease)

    assert counts(gleif, batch_id) == {
        "status": "COMPLETED", "records_scanned": 3, "records_selected": 3,
        "observations_inserted": 1, "observations_already_recorded": 0,
        "quarantined_inserted": 1, "quarantined_already_recorded": 0, "identical_duplicates": 1}
    assert gleif.rows("SELECT source_record_ordinal, repeats_ordinal FROM "
                      "gleif_batch_duplicate_occurrences WHERE batch_id = %s", (batch_id,)) == [
        {"source_record_ordinal": 3, "repeats_ordinal": 2}]
    # Nothing trusted came from the quarantined record.
    assert gleif.rows("SELECT lei FROM legal_entities WHERE lei = %s", (BAD_CHILD,)) == []
    assert gleif.rows("SELECT 1 FROM corporate_relationships WHERE child_lei = %s", (BAD_CHILD,)) == []
    labels = coverage_properties("COMPLETED", "ALL_RECORDS", "FULL", quarantined_records=1)
    assert not labels["trusted_complete_dataset"]


def test_completion_is_checked_against_rows_not_counters(gleif, quarantine):
    file_id = gleif.source_file(records=1)
    batch_id, lease = gleif.start(file_id)
    quarantine.add(batch_id, file_id, 1, lease, link=False)
    quarantine.progress(batch_id, lease, 1, 1, q_inserted=1)
    expect(1644, gleif.complete, batch_id, lease)            # created but not a member
    expect(1644, quarantine.link, batch_id, file_id, gleif.rows(
        "SELECT quarantine_id FROM gleif_quarantined_records WHERE batch_id = %s",
        (batch_id,))[0]["quarantine_id"], 1, lease)          # and too late to add it


def test_a_position_is_either_an_observation_or_quarantined(gleif, quarantine):
    file_id = gleif.source_file(records=2)
    batch_id, lease = gleif.start(file_id)
    gleif.observe(batch_id, file_id, 1, lease)
    quarantine.add(batch_id, file_id, 2, lease)

    expect(1644, quarantine.add, batch_id, file_id, 1, lease, untrusted("INACTIVE"))
    expect(1644, gleif.observe, batch_id, file_id, 2, lease, record("INACTIVE"))


def test_quarantine_is_append_only_and_lease_checked(gleif, quarantine):
    file_id = gleif.source_file(records=2)
    batch_id, lease = gleif.start(file_id)
    expect(1644, quarantine.add, batch_id, file_id, 1, uuid.uuid4().hex)
    quarantine_id = quarantine.add(batch_id, file_id, 1, lease)

    for statement in ("UPDATE gleif_quarantined_records SET subject_key = 'x' WHERE quarantine_id = %s",
                      "DELETE FROM gleif_quarantined_records WHERE quarantine_id = %s",
                      "UPDATE gleif_batch_quarantined_records SET source_record_ordinal = 1 "
                      "WHERE quarantine_id = %s",
                      "DELETE FROM gleif_batch_quarantined_records WHERE quarantine_id = %s"):
        expect(1644, gleif.run, statement, (quarantine_id,))


def test_conflicting_and_identical_quarantined_records(gleif, quarantine):
    file_id = gleif.source_file(records=3)
    batch_id, lease = gleif.start(file_id)
    quarantine.add(batch_id, file_id, 1, lease)
    quarantine.add(batch_id, file_id, 2, lease, untrusted("INACTIVE"))   # conflicting: kept
    expect(1062, quarantine.add, batch_id, file_id, 3, lease)            # identical: same row

    assert [r["source_record_ordinal"] for r in gleif.rows(
        "SELECT source_record_ordinal FROM gleif_quarantined_records WHERE source_file_id = %s "
        "ORDER BY source_record_ordinal", (file_id,))] == [1, 2]


def test_overlapping_first_n_and_lei_set_share_quarantined_records(gleif, quarantine):
    file_id = gleif.source_file(records=2)
    first_n, lease_f = gleif.start(file_id, canonical_selection("RR", "FIRST_N", limit=1))
    quarantine_id = quarantine.add(first_n, file_id, 1, lease_f)
    quarantine.progress(first_n, lease_f, 1, 1, q_inserted=1)
    gleif.complete(first_n, lease_f)

    lei_set, lease_l = gleif.start(file_id, canonical_selection("RR", "LEI_SET", leis=[PARENT],
                                                                 match="PARENT"))
    quarantine.link(lei_set, file_id, quarantine_id, 1, lease_l)
    gleif.observe(lei_set, file_id, 2, lease_l)
    quarantine.progress(lei_set, lease_l, 2, 2, inserted=1, q_already=1)
    gleif.complete(lei_set, lease_l)

    assert counts(gleif, first_n)["quarantined_inserted"] == 1
    assert (counts(gleif, lei_set)["quarantined_inserted"],
            counts(gleif, lei_set)["quarantined_already_recorded"]) == (0, 1)
    assert coverage_properties("COMPLETED", "FIRST_N", "FULL", 1)["label"] == "COMPLETE_SAMPLE_OF_FILE"


def test_counters_start_at_zero_never_decrease_and_freeze_when_completed(gleif, quarantine):
    file_id = gleif.source_file(records=1)
    batch_id, lease = gleif.start(file_id)
    quarantine.add(batch_id, file_id, 1, lease)
    quarantine.progress(batch_id, lease, 1, 1, q_inserted=1)
    expect(1644, quarantine.progress, batch_id, lease, 1, 1, inserted=1)   # q goes backwards
    gleif.complete(batch_id, lease)
    expect(1644, quarantine.progress, batch_id, lease, 1, 1, q_inserted=1)

    other = gleif.source_file("another file", records=1)
    token = uuid.uuid4().hex
    expect(1644, gleif.run, "INSERT INTO gleif_import_batches (source_file_id, selection_mode, "
           "selection_json, selection_fingerprint, selection_description, parser_version, "
           "record_hash_version, lease_token, lease_expires_at_utc, started_by, "
           "quarantined_inserted) VALUES (%s, 'ALL_RECORDS', '{\"mode\": \"ALL_RECORDS\"}', %s, "
           "'x', 'p', 1, %s, UTC_TIMESTAMP(6) + INTERVAL 1 MINUTE, 't', 1)",
           (other, "a" * 64, token), lease=token)


def test_stale_worker_cannot_quarantine_after_takeover(gleif, quarantine):
    file_id = gleif.source_file(records=2)
    batch_id, stale = gleif.start(file_id, ttl=1)
    quarantine.add(batch_id, file_id, 1, stale)
    quarantine.progress(batch_id, stale, 1, 1, q_inserted=1)
    gleif.run("UPDATE gleif_import_batches SET lease_expires_at_utc = UTC_TIMESTAMP(6) + "
              "INTERVAL 1 SECOND WHERE batch_id = %s", (batch_id,), lease=stale)
    time.sleep(1.5)
    expect(1644, quarantine.add, batch_id, file_id, 2, stale, untrusted("INACTIVE"))
    fresh = uuid.uuid4().hex
    assert gleif.take_over(batch_id, fresh) == 1
    expect(1644, quarantine.add, batch_id, file_id, 2, stale, untrusted("INACTIVE"))
    quarantine.add(batch_id, file_id, 2, fresh, untrusted("INACTIVE"))
    quarantine.progress(batch_id, fresh, 2, 2, q_inserted=2)
    gleif.complete(batch_id, fresh)


def test_zero_quarantine_batches_keep_their_original_accounting(gleif, quarantine):
    file_id = gleif.source_file(records=2)
    batch_id, lease = gleif.start(file_id)
    gleif.observe(batch_id, file_id, 1, lease)
    gleif.observe(batch_id, file_id, 2, lease, record("INACTIVE"))
    gleif.progress(batch_id, lease, 2, 2, 2)                 # the 23B.2B-1 progress update
    gleif.complete(batch_id, lease)
    assert counts(gleif, batch_id)["quarantined_inserted"] == 0


def test_source_file_header_metadata_and_entity_canonical_record(gleif, quarantine):
    from gleif_rr_samples import LOU

    extension = json.dumps({"namespace": "urn:x", "sources": []})
    gleif.run("INSERT INTO gleif_source_files (dataset, file_sha256, file_size_bytes, file_name, "
              "content_date_text, content_date_utc, publication_scope, record_count, registered_by, "
              "originator, header_extension, header_source_counts_consistent) VALUES ('RR', %s, 1, "
              "'synthetic-meta.xml', '2026-10-22T09:00:01Z', '2026-10-22 09:00:01', 'FULL', 0, "
              "'synthetic test', %s, %s, TRUE)", ("b" * 64, LOU, extension))
    gleif.files += [r["source_file_id"] for r in gleif.rows(
        "SELECT source_file_id FROM gleif_source_files WHERE file_sha256 = %s", ("b" * 64,))]
    expect(3819, gleif.run, "INSERT INTO gleif_source_files (dataset, file_sha256, file_size_bytes, "
           "file_name, content_date_text, content_date_utc, publication_scope, record_count, "
           "registered_by, header_extension) VALUES ('RR', %s, 1, 'synthetic-bad.xml', "
           "'2026-10-22T09:00:01Z', '2026-10-22 09:00:01', 'FULL', 0, 'synthetic test', %s)",
           ("c" * 64, extension))                            # consistency result missing

    file_id = gleif.source_file("lei2", dataset="LEI2", records=1)
    batch_id, lease = gleif.start(file_id, canonical_selection("LEI2", "ALL_RECORDS"))
    expect(3819, gleif.run, "INSERT INTO legal_entity_observations (source_file_id, batch_id, "
           "source_record_ordinal, record_hash_version, record_sha256, lei) "
           "VALUES (%s, %s, 1, 1, %s, %s)", (file_id, batch_id, "d" * 64, CHILD), lease=lease)


def test_final_trigger_order(gleif, quarantine):
    triggers = gleif.rows("SELECT EVENT_OBJECT_TABLE, EVENT_MANIPULATION, ACTION_ORDER, TRIGGER_NAME "
                          "FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE() AND "
                          "EVENT_OBJECT_TABLE IN ('gleif_import_batches', 'relationship_observations', "
                          "'legal_entity_observations') AND ACTION_TIMING = 'BEFORE'")
    if not triggers:
        pytest.skip("This account cannot see triggers (TRIGGER privilege needed)")
    order = {(t["EVENT_OBJECT_TABLE"], t["EVENT_MANIPULATION"]): [] for t in triggers}
    for t in sorted(triggers, key=lambda t: t["ACTION_ORDER"]):
        order[(t["EVENT_OBJECT_TABLE"], t["EVENT_MANIPULATION"])].append(t["TRIGGER_NAME"])
    assert order[("gleif_import_batches", "INSERT")] == ["trg_gleif_batches_start_v2"]
    assert order[("gleif_import_batches", "UPDATE")] == ["trg_gleif_batches_lifecycle_v2"]
    assert order[("relationship_observations", "INSERT")] == [
        "trg_relationship_observations_batch", "trg_relationship_observations_not_quarantined"]
    assert order[("legal_entity_observations", "INSERT")] == [
        "trg_entity_observations_batch", "trg_entity_observations_not_quarantined"]


def test_concurrent_batches_cannot_make_one_position_trusted_and_quarantined(gleif, quarantine):
    """The source-position key serializes writers; a NOT EXISTS check alone could not."""

    import threading
    from mysql.connector import errors

    file_id = gleif.source_file(records=1)
    first, lease_a = gleif.start(file_id, canonical_selection("RR", "FIRST_N", limit=1))
    second, lease_b = gleif.start(file_id)

    holder = connect()                                      # batch A: observation, uncommitted
    cursor = holder.cursor()
    cursor.execute("SET @crie_gleif_lease = %s", (lease_a,))
    fields = record()
    cursor.execute("INSERT INTO relationship_observations (source_file_id, batch_id, "
                   "source_record_ordinal, record_hash_version, record_sha256, child_lei, parent_lei, "
                   "relationship_type) VALUES (%s, %s, 1, 1, %s, %s, %s, %s)",
                   (file_id, first, record_sha256(fields), *KEY))
    outcome = []

    def quarantine_same_position():
        try:
            quarantine.add(second, file_id, 1, lease_b, link=False)
            outcome.append("inserted")
        except errors.DatabaseError as error:
            outcome.append((error.errno, error.msg))

    worker = threading.Thread(target=quarantine_same_position)
    worker.start()
    worker.join(timeout=2)
    assert worker.is_alive()                               # waiting on the position key
    holder.commit()
    worker.join(timeout=60)
    cursor.execute("SET @crie_gleif_lease = NULL")
    cursor.close()
    holder.close()

    # The second writer waits for the first; after it commits, the second is
    # refused (by the exclusivity trigger, or else by the position key).
    assert outcome in ([(1644, "a source position cannot be both an observation and quarantined")],
                       [(1062, outcome[0][1])])
    assert gleif.rows("SELECT kind FROM gleif_source_positions WHERE source_file_id = %s",
                      (file_id,)) == [{"kind": "OBSERVATION"}]


def test_a_batch_position_has_exactly_one_role(gleif, quarantine):
    file_id = gleif.source_file(records=3)
    batch_id, lease = gleif.start(file_id)
    gleif.observe(batch_id, file_id, 1, lease)
    gleif.duplicate(batch_id, file_id, 2, 1, lease, record_sha256(record()))
    # Position 2 is a duplicate in this batch: it cannot also be a membership.
    quarantined = quarantine.add(batch_id, file_id, 2, lease, link=False)
    expect(1062, quarantine.link, batch_id, file_id, quarantined, 2, lease)

    assert [r["kind"] for r in gleif.rows(
        "SELECT kind FROM gleif_batch_positions WHERE batch_id = %s ORDER BY source_record_ordinal",
        (batch_id,))] == ["TRUSTED", "DUPLICATE"]
