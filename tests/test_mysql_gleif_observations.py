
"""
Historical GLEIF observations against the real test database
(corporate_intelligence_test only). Files, records and LEIs are synthetic;
nothing is parsed from a real GLEIF file. Raw SQL shows the database itself
enforces each identity, lifecycle and append-only rule.
"""

import datetime
import json
import hashlib
import threading
import time
import uuid

import pytest
from mysql.connector import errors

from src.evidence_snapshot import as_version, snapshot_sha256
from src.gleif_observations import (
    absence_meaning,
    batch_coverage,
    canonical_bytes,
    canonical_selection,
    record_sha256,
    selection_fingerprint,
)

from test_mysql_evidence_snapshot import (  # noqa: F401
    block_default_connections,
    connect,
    pytestmark,
)
from test_mysql_report_registry import registry_db, seed, snapshot_now  # noqa: F401


def synthetic_lei(prefix):
    base = (prefix + "0" * 18)[:18]
    check = 98 - int("".join(str(int(c, 36)) for c in base + "00")) % 97
    return f"{base}{check:02d}"


CHILD, PARENT = synthetic_lei("SYNTHOBSC"), synthetic_lei("SYNTHOBSP")
KEY = (CHILD, PARENT, "IS_DIRECTLY_CONSOLIDATED_BY")
REVERSED = (PARENT, CHILD, "IS_DIRECTLY_CONSOLIDATED_BY")   # a second, synthetic relationship
CONTENT_DATE = "2026-10-22T09:00:01.250Z"
CONTENT_UTC = datetime.datetime(2026, 10, 22, 9, 0, 1, 250000)
ANCHOR_AT = datetime.datetime(2026, 10, 22, 9, 0, 1)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def record(status="ACTIVE", validation="ENTITY_SUPPLIED_ONLY", registration="PUBLISHED", key=KEY):
    return {"child_lei": key[0], "parent_lei": key[1], "relationship_type": key[2],
            "relationship_status": status, "registration_status": registration,
            "validation_sources": validation}


def expect(errno, action, *args, **kwargs):
    with pytest.raises(errors.DatabaseError) as raised:
        action(*args, **kwargs)
    assert raised.value.errno == errno, raised.value
    return raised.value


class Gleif:
    """Raw-SQL steps of an import, one committed transaction each."""

    def __init__(self, connection):
        self.connection = connection
        self.files = []

    def run(self, sql, params=(), lease=None):
        cursor = self.connection.cursor()
        try:
            cursor.execute("SET @crie_gleif_lease = %s", (lease,))
            cursor.execute(sql, params)
            self.connection.commit()
            return cursor.lastrowid, cursor.rowcount
        except Exception:
            self.connection.rollback()
            raise
        finally:
            cursor.close()

    def source_file(self, content="file one", name="synthetic-rr.xml", records=3,
                    scope="FULL", dataset="RR", content_date=CONTENT_DATE, content_utc=CONTENT_UTC):
        file_id, _ = self.run(
            "INSERT INTO gleif_source_files (dataset, file_sha256, file_size_bytes, file_name, "
            "content_date_text, content_date_utc, publication_scope, delta_basis, record_count, "
            "registered_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'synthetic test')",
            (dataset, sha(content), len(content), name, content_date, content_utc, scope,
             "2026-10-21T09:00:00Z" if scope == "DELTA" else None, records))
        self.files.append(file_id)
        return file_id

    def start(self, file_id, selection=None, lease=None, ttl=600):
        selection = selection or canonical_selection("RR", "ALL_RECORDS")
        lease = lease or uuid.uuid4().hex
        batch_id, _ = self.run(
            "INSERT INTO gleif_import_batches (source_file_id, selection_mode, selection_limit, "
            "selection_json, selection_fingerprint, selection_description, parser_version, "
            "record_hash_version, lease_token, lease_expires_at_utc, started_by) "
            "VALUES (%s, %s, %s, %s, %s, 'Synthetic selection', 'synthetic-1', 1, %s, "
            "UTC_TIMESTAMP(6) + INTERVAL %s MICROSECOND, 'synthetic test')",
            (file_id, selection["mode"], selection.get("limit"), canonical_bytes(selection).decode(),
             selection_fingerprint(selection), lease, int(ttl * 1e6)), lease=lease)
        return batch_id, lease

    def observe(self, batch_id, file_id, ordinal, lease, fields=None, periods=(), link=True):
        """Create an observation (and, as the importer does, its membership)."""

        fields = fields or record()
        observation_id, _ = self.run(
            "INSERT INTO relationship_observations (source_file_id, batch_id, "
            "source_record_ordinal, record_hash_version, record_sha256, child_lei, parent_lei, "
            "relationship_type, relationship_status, registration_status, validation_sources) "
            "VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s, %s, %s)",
            (file_id, batch_id, ordinal, record_sha256(fields),
             fields["child_lei"], fields["parent_lei"], fields["relationship_type"],
             fields["relationship_status"], fields["registration_status"],
             fields["validation_sources"]), lease=lease)
        for index, (kind, start, end) in enumerate(periods):
            self.run("INSERT INTO relationship_observation_periods (observation_id, period_index, "
                     "period_type, start_text, start_at_utc, end_text, end_at_utc) "
                     "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                     (observation_id, index, kind, start, start, end, end), lease=lease)
        if link:
            self.link(batch_id, file_id, observation_id, ordinal, lease)
        return observation_id

    def link(self, batch_id, file_id, observation_id, ordinal, lease):
        self.run("INSERT INTO relationship_batch_observations (batch_id, observation_id, "
                 "source_file_id, source_record_ordinal) VALUES (%s, %s, %s, %s)",
                 (batch_id, observation_id, file_id, ordinal), lease=lease)

    def observation_at(self, file_id, ordinal):
        (row,) = self.rows("SELECT observation_id FROM relationship_observations "
                           "WHERE source_file_id = %s AND source_record_ordinal = %s",
                           (file_id, ordinal))
        return row["observation_id"]

    def progress(self, batch_id, lease, scanned, selected, inserted, already=0, duplicates=0,
                 ttl=600):
        """Commit progress and renew the lease, as the end of each chunk does."""

        return self.run(
            "UPDATE gleif_import_batches SET records_scanned = %s, records_selected = %s, "
            "observations_inserted = %s, observations_already_recorded = %s, "
            "identical_duplicates = %s, progress_at_utc = UTC_TIMESTAMP(6), "
            "lease_expires_at_utc = UTC_TIMESTAMP(6) + INTERVAL %s MICROSECOND "
            "WHERE batch_id = %s",
            (scanned, selected, inserted, already, duplicates, int(ttl * 1e6), batch_id),
            lease=lease)[1]

    def complete(self, batch_id, lease):
        return self.run("UPDATE gleif_import_batches SET status = 'COMPLETED', "
                        "completed_at_utc = UTC_TIMESTAMP(6) WHERE batch_id = %s",
                        (batch_id,), lease=lease)[1]

    def fail(self, batch_id, lease=None, reason="Synthetic interruption"):
        return self.run("UPDATE gleif_import_batches SET status = 'FAILED', "
                        "failed_at_utc = UTC_TIMESTAMP(6), failure_reason = %s "
                        "WHERE batch_id = %s AND status = 'STARTED'", (reason, batch_id),
                        lease=lease)[1]

    def _relet(self, batch_id, lease, status, ttl):
        return self.run("UPDATE gleif_import_batches SET status = 'STARTED', lease_token = %s, "
                        "lease_expires_at_utc = UTC_TIMESTAMP(6) + INTERVAL %s MICROSECOND, "
                        "attempt = attempt + 1 WHERE batch_id = %s AND status = %s",
                        (lease, int(ttl * 1e6), batch_id, status), lease=lease)[1]

    def resume(self, batch_id, lease, ttl=600):
        return self._relet(batch_id, lease, "FAILED", ttl)

    def take_over(self, batch_id, lease, ttl=600):
        return self._relet(batch_id, lease, "STARTED", ttl)

    def anchor(self, batch_id=None, at=ANCHOR_AT, lease=None):
        return self.run(
            "INSERT INTO relationship_evidence (child_lei, parent_lei, relationship_type, "
            "snapshot_at_utc, validation_sources, validation_documents, periods_json, "
            "source_file, first_batch_id) VALUES (%s, %s, %s, %s, 'ENTITY_SUPPLIED_ONLY', "
            "'OTHER_OFFICIAL_DOCUMENTS', '[]', 'synthetic-rr.xml', %s)",
            (*KEY, at, batch_id), lease=lease)

    def installed(self, table):
        """Whether a table exists here (m23b2b3 adds some)."""

        return bool(self.rows("SELECT COUNT(*) AS n FROM information_schema.TABLES WHERE "
                              "TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (table,))[0]["n"])

    def duplicate(self, batch_id, file_id, ordinal, repeats, lease, digest):
        """Record an identical duplicate's position (required once m23b2b3 is installed)."""

        if self.installed("gleif_batch_duplicate_occurrences"):
            self.run("INSERT INTO gleif_batch_duplicate_occurrences (batch_id, source_file_id, "
                     "source_record_ordinal, repeats_ordinal, record_sha256) "
                     "VALUES (%s, %s, %s, %s, %s)", (batch_id, file_id, ordinal, repeats, digest),
                     lease=lease)

    def rows(self, sql, params=()):
        cursor = self.connection.cursor(dictionary=True)
        try:
            cursor.execute(sql, params)
            return cursor.fetchall()
        finally:
            cursor.close()
            self.connection.rollback()


@pytest.fixture
def gleif():
    connection = connect()
    cursor = connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
                   "AND TABLE_NAME = 'gleif_import_batches'")
    installed = cursor.fetchone()[0]
    connection.rollback()
    if not installed:
        cursor.close()
        connection.close()
        pytest.skip("GLEIF observations are not installed in the test database; apply "
                    "sql/test_schema_upgrade_m23b2b.sql with a privileged account "
                    "(test database only).")
    cursor.execute("INSERT INTO legal_entities (lei, legal_name) VALUES (%s, %s), (%s, %s)",
                   (CHILD, "Synthetic Observed Child a. s.", PARENT, "Synthetic Observed Parent"))
    cursor.execute("INSERT INTO corporate_relationships (child_lei, parent_lei, relationship_type, "
                   "relationship_status, registration_status) VALUES (%s, %s, %s, 'ACTIVE', "
                   "'PUBLISHED'), (%s, %s, %s, 'ACTIVE', 'PUBLISHED')", (*KEY, *REVERSED))
    connection.commit()
    cursor.close()
    tools = Gleif(connection)
    try:
        yield tools
    finally:
        connection.rollback()
        # Decided before deleting: installed() ends its own transaction.
        new_tables = [table for table in ("gleif_batch_positions", "gleif_source_positions",
                                          "gleif_batch_duplicate_occurrences",
                                          "gleif_batch_quarantined_records",
                                          "gleif_quarantined_records") if tools.installed(table)]
        cleanup = connection.cursor()
        cleanup.execute("SET @crie_gleif_maintenance = 1")
        cleanup.execute("DELETE FROM relationship_batch_observations WHERE observation_id IN "
                        "(SELECT observation_id FROM relationship_observations WHERE child_lei IN (%s, %s))",
                        (CHILD, PARENT))
        cleanup.execute("DELETE FROM entity_batch_observations WHERE observation_id IN "
                        "(SELECT observation_id FROM legal_entity_observations WHERE lei IN (%s, %s))",
                        (CHILD, PARENT))
        cleanup.execute("DELETE p FROM relationship_observation_periods p JOIN "
                        "relationship_observations o USING (observation_id) WHERE o.child_lei IN (%s, %s)",
                        (CHILD, PARENT))
        cleanup.execute("DELETE FROM relationship_observations WHERE child_lei IN (%s, %s)", (CHILD, PARENT))
        cleanup.execute("DELETE FROM legal_entity_observations WHERE lei IN (%s, %s)", (CHILD, PARENT))
        cleanup.execute("DELETE FROM relationship_evidence WHERE child_lei IN (%s, %s)", (CHILD, PARENT))
        if tools.files:
            slots = ", ".join(["%s"] * len(tools.files))
            for table in new_tables:
                if table == "gleif_batch_positions":         # keyed by batch, not file
                    cleanup.execute(f"DELETE p FROM gleif_batch_positions p JOIN gleif_import_batches b "
                                    f"USING (batch_id) WHERE b.source_file_id IN ({slots})", tools.files)
                else:
                    cleanup.execute(f"DELETE FROM {table} WHERE source_file_id IN ({slots})",
                                    tools.files)
            cleanup.execute(f"DELETE FROM gleif_import_batches WHERE source_file_id IN ({slots})",
                            tools.files)
            cleanup.execute(f"DELETE FROM gleif_source_files WHERE source_file_id IN ({slots})",
                            tools.files)
        cleanup.execute("SET @crie_gleif_maintenance = 0")
        cleanup.execute("DELETE FROM corporate_relationships WHERE child_lei IN (%s, %s)", (CHILD, PARENT))
        cleanup.execute("DELETE FROM legal_entities WHERE lei IN (%s, %s)", (CHILD, PARENT))
        connection.commit()
        cleanup.close()
        connection.close()


def members(gleif, batch_id):
    """The file ordinals of every observation a batch includes."""

    return [r["source_record_ordinal"] for r in gleif.rows(
        "SELECT o.source_record_ordinal FROM relationship_batch_observations m "
        "JOIN relationship_observations o USING (observation_id) WHERE m.batch_id = %s "
        "ORDER BY o.source_record_ordinal", (batch_id,))]


def import_whole_file(gleif, file_id, records, selection=None):
    """A complete import of `records` (all distinct), one chunk."""

    batch_id, lease = gleif.start(file_id, selection)
    for ordinal, fields in enumerate(records, start=1):
        gleif.observe(batch_id, file_id, ordinal, lease, fields)
    gleif.progress(batch_id, lease, len(records), len(records), len(records))
    gleif.complete(batch_id, lease)
    return batch_id


def test_two_files_sharing_a_content_date_are_both_recorded(gleif):
    first, second = gleif.source_file("file one"), gleif.source_file("file two")
    one = import_whole_file(gleif, first, [record(), record("INACTIVE", "x"), record("INACTIVE", "y")])
    two = import_whole_file(gleif, second, [record(validation="PARTIALLY_CORROBORATED"),
                                            record("INACTIVE", "x"), record("INACTIVE", "z")])

    observed = gleif.rows("SELECT batch_id, validation_sources FROM relationship_observations "
                          "WHERE child_lei = %s ORDER BY observation_id", (CHILD,))
    assert [o["batch_id"] for o in observed] == [one] * 3 + [two] * 3
    assert observed[3]["validation_sources"] == "PARTIALLY_CORROBORATED"
    files = gleif.rows("SELECT content_date_utc FROM gleif_source_files WHERE source_file_id IN "
                       "(%s, %s)", (first, second))
    assert {f["content_date_utc"] for f in files} == {CONTENT_UTC}


def test_identical_reimport_and_changed_content_under_the_same_name(gleif):
    gleif.source_file("file one", name="same-name.xml")

    # The same bytes are the same file, whatever it is called.
    expect(1062, gleif.source_file, "file one", name="renamed.xml")
    # Different bytes under the same name and date are a different file.
    changed = gleif.source_file("file one, corrected", name="same-name.xml")
    assert changed


def test_distinct_selections_from_one_file(gleif):
    file_id = gleif.source_file()
    sample = canonical_selection("RR", "FIRST_N", limit=1)
    first, lease = gleif.start(file_id, sample)
    gleif.observe(first, file_id, 1, lease)
    gleif.progress(first, lease, 1, 1, 1)

    # The same selection is the same batch...
    expect(1062, gleif.start, file_id, sample)
    # ...a different one is a new batch, which cannot record record 1 again
    # but records what the first selection did not.
    whole, whole_lease = gleif.start(file_id, canonical_selection("RR", "ALL_RECORDS"))
    expect(1062, gleif.observe, whole, file_id, 1, whole_lease)
    gleif.link(whole, file_id, gleif.observation_at(file_id, 1), 1, whole_lease)
    gleif.observe(whole, file_id, 2, whole_lease, record("INACTIVE"))
    gleif.observe(whole, file_id, 3, whole_lease, record("INACTIVE", "other"))
    gleif.progress(whole, whole_lease, 3, 3, 2, already=1)
    gleif.complete(whole, whole_lease)

    gleif.complete(first, lease)                    # FIRST_N 1 reads one record only

    batches = [o["batch_id"] for o in gleif.rows(
        "SELECT batch_id FROM relationship_observations WHERE source_file_id = %s "
        "ORDER BY source_record_ordinal", (file_id,))]
    assert batches == [first, whole, whole]
    assert members(gleif, first) == [1] and members(gleif, whole) == [1, 2, 3]


def test_repeated_records_for_one_relationship_within_a_file(gleif):
    file_id = gleif.source_file(records=3)
    batch_id, lease = gleif.start(file_id)
    gleif.observe(batch_id, file_id, 1, lease)

    # An identical repeat is the same observation (counted, not stored)...
    expect(1062, gleif.observe, batch_id, file_id, 2, lease)
    gleif.duplicate(batch_id, file_id, 2, 1, lease, record_sha256(record()))
    # ...a conflicting record is kept as its own observation.
    gleif.observe(batch_id, file_id, 3, lease, record("INACTIVE"))
    gleif.progress(batch_id, lease, 3, 3, 2, duplicates=1)
    gleif.complete(batch_id, lease)

    statuses = [o["relationship_status"] for o in gleif.rows(
        "SELECT relationship_status FROM relationship_observations WHERE batch_id = %s "
        "ORDER BY source_record_ordinal", (batch_id,))]
    assert statuses == ["ACTIVE", "INACTIVE"]


def test_interrupted_batch_resumes_from_committed_progress(gleif):
    file_id = gleif.source_file(records=3)
    batch_id, lease = gleif.start(file_id)
    gleif.observe(batch_id, file_id, 1, lease)
    gleif.progress(batch_id, lease, 1, 1, 1)

    # A chunk that never commits leaves nothing behind.
    cursor = gleif.connection.cursor()
    cursor.execute("SET @crie_gleif_lease = %s", (lease,))
    cursor.execute("INSERT INTO relationship_observations (source_file_id, batch_id, "
                   "source_record_ordinal, record_hash_version, record_sha256, child_lei, "
                   "parent_lei, relationship_type) VALUES (%s, %s, 2, 1, %s, %s, %s, %s)",
                   (file_id, batch_id, record_sha256(record("INACTIVE")), *KEY))
    gleif.connection.rollback()
    cursor.close()

    expect(1644, gleif.fail, batch_id)              # not the holder, lease still live
    assert gleif.fail(batch_id, lease) == 1
    expect(1644, gleif.observe, batch_id, file_id, 2, lease, record("INACTIVE"))
    new_lease = uuid.uuid4().hex
    assert gleif.resume(batch_id, new_lease) == 1
    # The old lease is dead, and committed records cannot be written again.
    expect(1644, gleif.observe, batch_id, file_id, 2, lease, record("INACTIVE"))
    expect(1644, gleif.observe, batch_id, file_id, 1, new_lease, record("RETIRED"))
    gleif.observe(batch_id, file_id, 2, new_lease, record("INACTIVE"))
    gleif.observe(batch_id, file_id, 3, new_lease, record(registration="RETIRED"))
    gleif.progress(batch_id, new_lease, 3, 3, 3)
    gleif.complete(batch_id, new_lease)

    (batch,) = gleif.rows("SELECT status, attempt, observations_inserted FROM gleif_import_batches "
                          "WHERE batch_id = %s", (batch_id,))
    assert batch == {"status": "COMPLETED", "attempt": 2, "observations_inserted": 3}


def test_progress_and_lifecycle_rules(gleif):
    file_id = gleif.source_file(records=2)
    batch_id, lease = gleif.start(file_id)
    gleif.observe(batch_id, file_id, 1, lease)

    expect(1644, gleif.progress, batch_id, uuid.uuid4().hex, 1, 1, 1)      # not the holder
    expect(3819, gleif.progress, batch_id, lease, 1, 1, 0)                 # counts disagree
    expect(1644, gleif.progress, batch_id, lease, 3, 1, 1)                 # beyond the file
    gleif.progress(batch_id, lease, 1, 1, 1)
    expect(1644, gleif.progress, batch_id, lease, 0, 0, 0)                 # backwards
    expect(1644, gleif.complete, batch_id, lease)                          # not fully scanned
    gleif.progress(batch_id, lease, 2, 2, 2)                               # claims a row it lacks
    expect(1644, gleif.complete, batch_id, lease)
    expect(1644, gleif.observe, batch_id, file_id, 2, lease, record("INACTIVE"))
    assert gleif.fail(batch_id, lease, "Synthetic: progress over-claimed") == 1
    (batch,) = gleif.rows("SELECT status FROM gleif_import_batches WHERE batch_id = %s", (batch_id,))
    assert batch["status"] == "FAILED"


def test_concurrent_attempts_to_import_the_same_batch(gleif):
    file_id = gleif.source_file(records=1)
    selection = canonical_selection("RR", "ALL_RECORDS")
    barrier, outcomes = threading.Barrier(2), []

    def attempt():
        own = Gleif(connect())
        try:
            barrier.wait()
            outcomes.append(own.start(file_id, selection))
        except errors.DatabaseError as error:
            outcomes.append(error)
        finally:
            own.connection.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    (winner,) = [o for o in outcomes if isinstance(o, tuple)]
    (loser,) = [o for o in outcomes if isinstance(o, errors.DatabaseError)]
    assert loser.errno == 1062

    # Without the lease, another importer can neither write nor finish it.
    batch_id, lease = winner
    expect(1644, gleif.observe, batch_id, file_id, 1, uuid.uuid4().hex)
    expect(1644, gleif.observe, batch_id, file_id, 1, None)
    gleif.observe(batch_id, file_id, 1, lease)
    gleif.progress(batch_id, lease, 1, 1, 1)
    expect(1644, gleif.complete, batch_id, uuid.uuid4().hex)
    gleif.complete(batch_id, lease)


def test_completed_batches_and_observations_are_immutable(gleif):
    file_id = gleif.source_file(records=1)
    batch_id, lease = gleif.start(file_id)
    # With a period, so the period UPDATE and DELETE below match a real row.
    gleif.observe(batch_id, file_id, 1, lease,
                  periods=[("ACCOUNTING_PERIOD", "2023-01-01", "2023-12-31")])
    gleif.progress(batch_id, lease, 1, 1, 1)
    gleif.complete(batch_id, lease)
    (observation,) = gleif.rows("SELECT observation_id FROM relationship_observations "
                                "WHERE batch_id = %s", (batch_id,))
    assert gleif.rows("SELECT COUNT(*) AS n FROM relationship_observation_periods "
                      "WHERE observation_id = %s", (observation["observation_id"],)) == [{"n": 1}]
    (lease,) = gleif.rows("SELECT lease_token FROM gleif_import_batches WHERE batch_id = %s",
                          (batch_id,))

    for statement in ("UPDATE gleif_import_batches SET failure_reason = 'x' WHERE batch_id = %s",
                      "UPDATE gleif_import_batches SET status = 'FAILED', failure_reason = 'x', "
                      "failed_at_utc = UTC_TIMESTAMP(6) WHERE batch_id = %s",
                      "DELETE FROM gleif_import_batches WHERE batch_id = %s"):
        expect(1644, gleif.run, statement, (batch_id,), lease=lease["lease_token"])
    expect(1644, gleif.observe, batch_id, file_id, 1, lease["lease_token"], record("INACTIVE"))
    for statement in ("UPDATE relationship_observations SET relationship_status = 'INACTIVE' "
                      "WHERE observation_id = %s",
                      "DELETE FROM relationship_observations WHERE observation_id = %s",
                      "UPDATE relationship_observation_periods SET period_type = 'x' "
                      "WHERE observation_id = %s",
                      "DELETE FROM relationship_observation_periods WHERE observation_id = %s"):
        expect(1644, gleif.run, statement, (observation["observation_id"],))
    for statement in ("UPDATE gleif_source_files SET file_name = 'x' WHERE source_file_id = %s",
                      "DELETE FROM gleif_source_files WHERE source_file_id = %s"):
        expect(1644, gleif.run, statement, (file_id,))


def test_status_validation_and_periods_are_kept_per_observation(gleif):
    batches = []
    for content, fields in (("jan", record()), ("feb", record("INACTIVE", "PARTIALLY_CORROBORATED")),
                            ("mar", record("INACTIVE", registration="RETIRED"))):
        file_id = gleif.source_file(content, records=1)
        batch_id, lease = gleif.start(file_id)
        gleif.observe(batch_id, file_id, 1, lease, fields, periods=[
            ("ACCOUNTING_PERIOD", "2023-01-01", "2023-12-31"),
            ("RELATIONSHIP_PERIOD", "2019-05-01", None)])
        gleif.progress(batch_id, lease, 1, 1, 1)
        gleif.complete(batch_id, lease)
        batches.append(batch_id)

    rows = gleif.rows(
        "SELECT o.batch_id, o.relationship_status, o.registration_status, o.validation_sources, "
        "p.period_type, p.start_at_utc, p.end_at_utc FROM relationship_observations o "
        "JOIN relationship_observation_periods p USING (observation_id) "
        "WHERE o.child_lei = %s ORDER BY o.observation_id, p.period_index", (CHILD,))
    assert [(r["relationship_status"], r["registration_status"]) for r in rows[::2]] == [
        ("ACTIVE", "PUBLISHED"), ("INACTIVE", "PUBLISHED"), ("INACTIVE", "RETIRED")]
    assert rows[2]["validation_sources"] == "PARTIALLY_CORROBORATED"
    # The accounting period stays 2023 whatever the publication date; an
    # unstated end is NULL, not an end.
    assert {r["start_at_utc"].year for r in rows if r["period_type"] == "ACCOUNTING_PERIOD"} == {2023}
    assert all(r["end_at_utc"] is None for r in rows if r["period_type"] == "RELATIONSHIP_PERIOD")
    # The identity table keeps its first-seen status.
    (identity,) = gleif.rows("SELECT relationship_status FROM corporate_relationships WHERE "
                             "child_lei = %s", (CHILD,))
    assert identity["relationship_status"] == "ACTIVE"


def test_entity_observations_keep_every_name(gleif):
    file_id = gleif.source_file("lei2", dataset="LEI2", records=2)
    selection = canonical_selection("LEI2", "ALL_RECORDS")
    batch_id, lease = gleif.start(file_id, selection)
    # After m23b2b3 every entity observation carries its canonical record.
    extra = gleif.rows("SELECT COUNT(*) AS n FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = "
                       "DATABASE() AND TABLE_NAME = 'legal_entity_observations' "
                       "AND COLUMN_NAME = 'canonical_record'")[0]["n"]
    for ordinal, name in ((1, "Synthetic Observed Child a. s."), (2, "SYNTHETIC OBSERVED CHILD A. S.")):
        fields = {"lei": CHILD, "legal_name": name}
        columns, values = "", ()
        if extra:
            columns, values = ", canonical_record, identifier_check", (json.dumps(fields), "VALID")
        observation_id, _ = gleif.run(
            "INSERT INTO legal_entity_observations (source_file_id, batch_id, "
            "source_record_ordinal, record_hash_version, record_sha256, lei, legal_name, "
            f"other_names{columns}) VALUES (%s, %s, %s, 1, %s, %s, %s, %s"
            + ", %s" * len(values) + ")",
            (file_id, batch_id, ordinal, record_sha256(fields),
             CHILD, name, '[{"name": "Synthetic Child", "type": "TRADING_OR_OPERATING_NAME"}]',
             *values), lease=lease)
        gleif.run("INSERT INTO entity_batch_observations (batch_id, observation_id, source_file_id, "
                  "source_record_ordinal) VALUES (%s, %s, %s, %s)",
                  (batch_id, observation_id, file_id, ordinal), lease=lease)
    # An RR observation cannot go into an LEI2 batch.
    expect(1644, gleif.observe, batch_id, file_id, 1, lease)
    gleif.progress(batch_id, lease, 2, 2, 2)
    gleif.complete(batch_id, lease)

    names = [r["legal_name"] for r in gleif.rows(
        "SELECT legal_name FROM legal_entity_observations WHERE lei = %s ORDER BY observation_id",
        (CHILD,))]
    assert names == ["Synthetic Observed Child a. s.", "SYNTHETIC OBSERVED CHILD A. S."]
    (entity,) = gleif.rows("SELECT legal_name FROM legal_entities WHERE lei = %s", (CHILD,))
    assert entity["legal_name"] == "Synthetic Observed Child a. s."


def test_anchors_legacy_null_and_first_batch(gleif):
    # A legacy-style anchor: no batch, no lease needed, and it stays NULL.
    gleif.anchor(at=datetime.datetime(2026, 1, 1))
    expect(1644, gleif.run, "UPDATE relationship_evidence SET first_batch_id = 1 "
           "WHERE child_lei = %s", (CHILD,))
    gleif.run("UPDATE relationship_evidence SET validation_reference = 'kept' WHERE child_lei = %s",
              (CHILD,))

    first, second = gleif.source_file("file one"), gleif.source_file("file two")
    one, lease = gleif.start(first)
    expect(1644, gleif.anchor, one, lease=uuid.uuid4().hex)                   # not the holder
    expect(1644, gleif.anchor, one, at=CONTENT_UTC.replace(second=2), lease=lease)
    gleif.anchor(one, lease=lease)                                            # 09:00:01
    two, lease_two = gleif.start(second)
    # A second file with the same date cannot replace the anchor; its
    # observations are recorded instead.
    expect(1062, gleif.anchor, two, lease=lease_two)

    anchors = gleif.rows("SELECT snapshot_at_utc, first_batch_id, validation_reference FROM "
                         "relationship_evidence WHERE child_lei = %s ORDER BY snapshot_at_utc",
                         (CHILD,))
    assert [(a["first_batch_id"], a["validation_reference"]) for a in anchors] == [
        (None, "kept"), (one, None)]


def test_existing_keys_and_foreign_keys_are_preserved(gleif):
    keys = gleif.rows(
        "SELECT CONSTRAINT_NAME, REFERENCED_TABLE_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY "
        "ORDINAL_POSITION) AS columns FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'relationship_evidence' "
        "GROUP BY CONSTRAINT_NAME, REFERENCED_TABLE_NAME ORDER BY CONSTRAINT_NAME")
    assert [(k["CONSTRAINT_NAME"], k["REFERENCED_TABLE_NAME"], k["columns"]) for k in keys] == [
        ("fk_evidence_first_batch", "gleif_import_batches", "first_batch_id"),
        ("fk_evidence_relationship", "corporate_relationships",
         "child_lei,parent_lei,relationship_type"),
        ("PRIMARY", None, "child_lei,parent_lei,relationship_type,snapshot_at_utc"),
    ]
    # No new table is scoped to, or can see into, an investigation.
    columns = gleif.rows(
        "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = "
        "DATABASE() AND TABLE_NAME IN ('gleif_source_files', 'gleif_import_batches', "
        "'relationship_observations', 'relationship_observation_periods', "
        "'legal_entity_observations') AND (COLUMN_NAME LIKE '%%case%%' OR COLUMN_NAME LIKE "
        "'%%source_id%%')")
    assert columns == []


def test_observations_do_not_change_snapshot_versions_1_to_3(registry_db, gleif):
    _, fixtures, _ = registry_db
    case_id, *_ = seed(fixtures)
    before = snapshot_now(case_id)

    import_whole_file(gleif, gleif.source_file(), [record(), record("INACTIVE"), record("x", "y")])
    after = snapshot_now(case_id)

    for version in ("1", "2", "3"):
        assert snapshot_sha256(as_version(before, version)) == snapshot_sha256(as_version(after, version))
    assert "observation" not in str(after)


def test_overlapping_first_n_and_lei_set_selections_keep_full_membership(gleif):
    # 1: KEY active   2: REVERSED   3: KEY inactive (conflicts with 1)   4: repeat of 2
    file_id = gleif.source_file(records=4)
    one, two = record(), record(key=REVERSED)
    three = record("INACTIVE")

    first_n, lease_f = gleif.start(file_id, canonical_selection("RR", "FIRST_N", limit=2))
    gleif.observe(first_n, file_id, 1, lease_f, one)
    gleif.observe(first_n, file_id, 2, lease_f, two)
    expect(1644, gleif.observe, first_n, file_id, 3, lease_f, three)          # beyond its limit
    expect(3819, gleif.progress, first_n, lease_f, 3, 3, 2, duplicates=1)     # scans past it
    gleif.progress(first_n, lease_f, 2, 2, 2)
    gleif.complete(first_n, lease_f)                 # records 3 and 4 are never read

    lei_set, lease_l = gleif.start(file_id, canonical_selection("RR", "LEI_SET", leis=[CHILD],
                                                                 match="CHILD"))
    gleif.link(lei_set, file_id, gleif.observation_at(file_id, 1), 1, lease_l)
    gleif.observe(lei_set, file_id, 3, lease_l, three)
    gleif.progress(lei_set, lease_l, 3, 2, 1, already=1)
    expect(1644, gleif.complete, lei_set, lease_l)   # a match could still be in record 4
    gleif.progress(lei_set, lease_l, 4, 2, 1, already=1)
    gleif.complete(lei_set, lease_l)

    whole, lease_a = gleif.start(file_id)
    # A membership cannot claim an observation before the record that created it.
    expect(1644, gleif.link, whole, file_id, gleif.observation_at(file_id, 3), 2, lease_a)
    for ordinal in (1, 2, 3):
        gleif.link(whole, file_id, gleif.observation_at(file_id, ordinal), ordinal, lease_a)
    expect(1062, gleif.observe, whole, file_id, 4, lease_a, two)             # identical repeat
    gleif.duplicate(whole, file_id, 4, 2, lease_a, record_sha256(two))
    gleif.progress(whole, lease_a, 4, 4, 0, already=3, duplicates=1)
    gleif.complete(whole, lease_a)

    assert members(gleif, first_n) == [1, 2]
    assert members(gleif, lei_set) == [1, 3]
    assert members(gleif, whole) == [1, 2, 3]
    created = {r["source_record_ordinal"]: r["batch_id"] for r in gleif.rows(
        "SELECT source_record_ordinal, batch_id FROM relationship_observations "
        "WHERE source_file_id = %s", (file_id,))}
    assert created == {1: first_n, 2: first_n, 3: lei_set}
    batches = {r["batch_id"]: r for r in gleif.rows(
        "SELECT b.batch_id, b.status, b.selection_mode, f.publication_scope FROM "
        "gleif_import_batches b JOIN gleif_source_files f USING (source_file_id) "
        "WHERE b.source_file_id = %s", (file_id,))}
    coverage = {i: batch_coverage(b["status"], b["selection_mode"], b["publication_scope"])
                for i, b in batches.items()}
    assert coverage == {first_n: "COMPLETE_SAMPLE_OF_FILE", lei_set: "COMPLETE_SAMPLE_OF_FILE",
                        whole: "COMPLETE_PUBLISHED_DATASET"}
    # Absence from the samples is not an end of anything.
    sample = canonical_selection("RR", "FIRST_N", limit=2)
    assert absence_meaning("FULL", sample) == "ABSENT_FROM_SAMPLE"


def test_completion_requires_every_membership(gleif):
    file_id = gleif.source_file(records=1)
    batch_id, lease = gleif.start(file_id)
    gleif.observe(batch_id, file_id, 1, lease, link=False)
    gleif.progress(batch_id, lease, 1, 1, 1)

    expect(1644, gleif.complete, batch_id, lease)     # created but not included
    expect(1644, gleif.link, batch_id, file_id, gleif.observation_at(file_id, 1), 1, lease)


def test_a_live_lease_cannot_be_failed_taken_over_or_used_by_others(gleif):
    file_id = gleif.source_file(records=2)
    batch_id, lease = gleif.start(file_id)
    other = uuid.uuid4().hex

    expect(1644, gleif.fail, batch_id)
    expect(1644, gleif.fail, batch_id, other)
    expect(1644, gleif.take_over, batch_id, other)
    expect(1644, gleif.observe, batch_id, file_id, 1, other)
    expect(1644, gleif.progress, batch_id, other, 0, 0, 0)
    # Renewal is the holder's alone and never more than an hour ahead.
    expect(1644, gleif.progress, batch_id, lease, 0, 0, 0, ttl=7200)
    assert gleif.progress(batch_id, lease, 0, 0, 0, ttl=1800) == 1
    expect(1644, gleif.start, gleif.source_file("long lease"), ttl=7200)


def test_expired_lease_fences_the_stale_worker_and_allows_takeover(gleif):
    file_id = gleif.source_file(records=2)
    batch_id, stale = gleif.start(file_id, ttl=1)
    gleif.observe(batch_id, file_id, 1, stale)
    gleif.progress(batch_id, stale, 1, 1, 1, ttl=1)
    time.sleep(1.5)

    # The stale worker can no longer write, renew or finish.
    expect(1644, gleif.observe, batch_id, file_id, 2, stale, record("INACTIVE"))
    expect(1644, gleif.progress, batch_id, stale, 1, 1, 1)
    expect(1644, gleif.complete, batch_id, stale)

    fresh = uuid.uuid4().hex
    assert gleif.take_over(batch_id, fresh) == 1
    # Now the stale token is fenced even though its worker may come back.
    expect(1644, gleif.observe, batch_id, file_id, 2, stale, record("INACTIVE"))
    expect(1644, gleif.observe, batch_id, file_id, 1, fresh, record("RETIRED"))  # committed
    gleif.observe(batch_id, file_id, 2, fresh, record("INACTIVE"))
    gleif.progress(batch_id, fresh, 2, 2, 2)
    gleif.complete(batch_id, fresh)
    (batch,) = gleif.rows("SELECT status, attempt, lease_token FROM gleif_import_batches "
                          "WHERE batch_id = %s", (batch_id,))
    assert batch == {"status": "COMPLETED", "attempt": 2, "lease_token": fresh}


def test_expired_lease_may_be_failed_by_anyone_then_resumed(gleif):
    file_id = gleif.source_file(records=1)
    batch_id, stale = gleif.start(file_id, ttl=1)
    time.sleep(1.5)

    assert gleif.fail(batch_id, reason="Synthetic: worker vanished") == 1
    expect(1644, gleif.resume, batch_id, stale)       # resuming needs a new token
    fresh = uuid.uuid4().hex
    assert gleif.resume(batch_id, fresh) == 1
    gleif.observe(batch_id, file_id, 1, fresh)
    gleif.progress(batch_id, fresh, 1, 1, 1)
    gleif.complete(batch_id, fresh)


def test_concurrent_takeover_has_a_single_winner(gleif):
    file_id = gleif.source_file(records=1)
    batch_id, _ = gleif.start(file_id, ttl=1)
    time.sleep(1.5)
    barrier, outcomes = threading.Barrier(2), []

    def attempt():
        own = Gleif(connect())
        try:
            barrier.wait()
            outcomes.append(own.take_over(batch_id, uuid.uuid4().hex))
        except errors.DatabaseError as error:
            outcomes.append(error)
        finally:
            own.connection.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(o if isinstance(o, int) else o.errno for o in outcomes) == [1, 1644]
    (batch,) = gleif.rows("SELECT attempt FROM gleif_import_batches WHERE batch_id = %s",
                          (batch_id,))
    assert batch["attempt"] == 2


def test_memberships_are_append_only(gleif):
    file_id = gleif.source_file(records=1)
    batch_id = import_whole_file(gleif, file_id, [record()])
    observation_id = gleif.observation_at(file_id, 1)

    for statement in ("UPDATE relationship_batch_observations SET source_record_ordinal = 1 "
                      "WHERE observation_id = %s",
                      "DELETE FROM relationship_batch_observations WHERE observation_id = %s"):
        expect(1644, gleif.run, statement, (observation_id,))
    assert members(gleif, batch_id) == [1]
