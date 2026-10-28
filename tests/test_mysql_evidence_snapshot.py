
import os
import uuid
from pathlib import Path

import mysql.connector
import pytest
from dotenv import load_dotenv

from src.claim_corrections import correct_claim
from src.evidence_snapshot import load_evidence_snapshot, snapshot_sha256
from src.generate_versioned_report import generate_versioned_report, verify_run


ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE = "corporate_intelligence_test"

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_CRI_DB_TEST") != "1",
    reason="Requires explicit integration-test opt-in",
)

# Columns the snapshot reads beyond the original test schema.
SNAPSHOT_COLUMNS = {
    ("case_sources", "source_url"),
    ("case_sources", "evidence_assessment"),
    ("disclosure_claims", "created_at"),
    ("claim_corrections", "corrected_at_utc"),
    ("source_assessment_history", "history_id"),
    ("source_history_annotations", "annotation_id"),
    ("source_metadata_history", "change_id"),
    ("v_current_disclosure_claims", "created_at"),
}


@pytest.fixture(autouse=True)
def block_default_connections(monkeypatch):
    """
    The application's default connection targets the .env
    database (production). Fail loudly if any code path in
    these tests falls back to it.
    """

    def refuse():
        raise AssertionError(
            "Default (production) connection attempted in a "
            "test-database integration test"
        )

    monkeypatch.setattr(
        "src.load_current_disclosures._connect", refuse
    )
    monkeypatch.setattr("src.evidence_snapshot._connect", refuse)


def connect():
    load_dotenv(ROOT / ".env")

    connection = mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=TEST_DATABASE,
    )

    cursor = connection.cursor()
    try:
        # Verify database identity before any insert.
        cursor.execute("SELECT DATABASE()")
        assert cursor.fetchone()[0] == TEST_DATABASE
        cursor.execute("SET SESSION time_zone = '+00:00'")
    finally:
        cursor.close()

    return connection


class Fixtures:
    """Fictional records created by one test, cleaned up after."""

    def __init__(self, connection):
        self.connection = connection
        self.cursor = connection.cursor()
        self.case_id = None
        self.source_ids = []

    def case(self):
        self.cursor.execute(
            "INSERT INTO investigation_cases (case_name) VALUES (%s)",
            ("Synthetic snapshot test",),
        )
        self.case_id = self.cursor.lastrowid
        return self.case_id

    def source(self, **columns):
        columns = {
            "case_id": self.case_id,
            "document_title": f"Fictional report {len(self.source_ids) + 1}",
            **columns,
        }
        names = ", ".join(columns)
        slots = ", ".join(["%s"] * len(columns))
        self.cursor.execute(
            f"INSERT INTO case_sources ({names}) VALUES ({slots})",
            tuple(columns.values()),
        )
        self.source_ids.append(self.cursor.lastrowid)
        return self.cursor.lastrowid

    def claim(self, source_id, year, value, cursor=None):
        cursor = cursor or self.cursor
        cursor.execute(
            """
            INSERT INTO disclosure_claims (
                source_id, entity_name, metric, report_year,
                period_end, value_text, normalized_value,
                source_location, claim_fingerprint
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                source_id, "Fictional Harbour Holdings plc",
                "harbour_interest_pct", year, f"{year}-12-31",
                value, value, "Page 3", uuid.uuid4().hex * 2,
            ),
        )
        return cursor.lastrowid

    def cleanup(self):
        self.connection.rollback()
        cursor = self.cursor

        try:
            if self.source_ids:
                slots = ", ".join(["%s"] * len(self.source_ids))
                ids = tuple(self.source_ids)

                if _has_history_tables(cursor):
                    cursor.execute(
                        f"DELETE a FROM source_history_annotations a "
                        f"JOIN source_assessment_history h "
                        f"ON h.history_id = a.history_id "
                        f"WHERE h.source_id IN ({slots})", ids,
                    )
                    cursor.execute(
                        f"DELETE FROM source_assessment_history "
                        f"WHERE source_id IN ({slots})", ids,
                    )

                # Corrections are append-only; the test schema allows
                # deleting synthetic rows only under this flag.
                cursor.execute("SET @crie_claim_maintenance = 1")
                cursor.execute(
                    f"DELETE cc FROM claim_corrections cc "
                    f"JOIN disclosure_claims c "
                    f"ON c.claim_id = cc.original_claim_id "
                    f"WHERE c.source_id IN ({slots})", ids,
                )
                cursor.execute("SET @crie_claim_maintenance = 0")
                cursor.execute(
                    f"DELETE FROM disclosure_claims "
                    f"WHERE source_id IN ({slots})", ids,
                )
                cursor.execute(
                    f"DELETE FROM case_sources "
                    f"WHERE source_id IN ({slots})", ids,
                )

            if self.case_id is not None:
                cursor.execute(
                    "DELETE FROM investigation_cases WHERE case_id = %s",
                    (self.case_id,),
                )

            self.connection.commit()
        finally:
            cursor.close()


def _has_history_tables(cursor):
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = 'source_assessment_history'"
    )
    return cursor.fetchone()[0] == 1


def _missing_snapshot_columns(connection):
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT TABLE_NAME, COLUMN_NAME "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE()"
        )
        present = set(cursor.fetchall())
    finally:
        cursor.close()

    return SNAPSHOT_COLUMNS - present


def test_consistent_snapshot_transaction_isolation():
    """
    The REPEATABLE READ consistent-snapshot transaction used
    by load_evidence_snapshot() must not see claims committed
    by another session after it starts.
    """

    reader = connect()
    writer = connect()
    fixtures = Fixtures(writer)

    try:
        fixtures.case()
        source_id = fixtures.source()
        fixtures.claim(source_id, 2024, "49")
        writer.commit()

        reader.start_transaction(
            consistent_snapshot=True,
            isolation_level="REPEATABLE READ",
            readonly=True,
        )

        def count():
            cursor = reader.cursor()
            try:
                cursor.execute(
                    "SELECT COUNT(*) FROM v_current_disclosure_claims "
                    "WHERE source_id = %s", (source_id,),
                )
                return cursor.fetchone()[0]
            finally:
                cursor.close()

        assert count() == 1

        fixtures.claim(source_id, 2025, "51")
        writer.commit()

        # Still the original snapshot.
        assert count() == 1

        reader.rollback()

        # A new transaction sees the committed change.
        assert count() == 2
        reader.rollback()

    finally:
        reader.close()
        try:
            fixtures.cleanup()
        finally:
            writer.close()


def test_snapshot_report_pipeline(tmp_path):
    writer = connect()
    missing = _missing_snapshot_columns(writer)

    if missing:
        writer.close()
        pytest.skip(
            "Test schema lacks snapshot columns "
            f"{sorted(missing)}; apply "
            "sql/test_schema_upgrade_m22_3a.sql with a privileged "
            "account (test database only)."
        )

    reader = connect()
    fixtures = Fixtures(writer)

    try:
        case_id = fixtures.case()
        a = fixtures.source(
            source_url="https://example.org/a.pdf?utm_source=x#page=2",
            evidence_assessment="SUPPORTS",
        )
        b = fixtures.source(source_url="https://example.org/b.pdf")

        cursor = writer.cursor()
        cursor.execute(
            """
            INSERT INTO source_assessment_history (
                source_id, event_type, previous_assessment,
                new_assessment, review_reason
            )
            VALUES (%s, 'REVIEW', 'NOT_REVIEWED', 'SUPPORTS', %s)
            """,
            (a, "Fictional review"),
        )
        cursor.close()

        c1 = fixtures.claim(a, 2024, "49")
        c2 = fixtures.claim(a, 2025, "49")
        c3 = fixtures.claim(b, 2025, "60")
        writer.commit()

        # Snapshot inside a caller-owned consistent transaction,
        # with a competing write committed in between.
        reader.start_transaction(
            consistent_snapshot=True,
            isolation_level="REPEATABLE READ",
            readonly=True,
        )
        fixtures.claim(b, 2024, "49")
        writer.commit()

        snapshot = load_evidence_snapshot(
            case_id=case_id, years=(2024, 2025), connection=reader
        )
        reader.rollback()

        assert [c["claim_id"] for c in snapshot["claims"]] == [c1, c2, c3]
        source_a = snapshot["sources"][0]
        assert source_a["evidence_assessment"] == "SUPPORTS"
        assert source_a["latest_assessment_history_id"] is not None

        result = generate_versioned_report(
            case_id=case_id,
            output_root=tmp_path,
            snapshot=snapshot,
            run_id="integration",
            code_version={"git_commit": None, "working_tree_dirty": None},
        )

        report = (result["run_dir"] / "report.md").read_text("utf-8")
        assert "UNRESOLVED_CONFLICT (CROSS_SOURCE)" in report
        assert f"- C{c2} from S{a}: 49%" in report
        assert f"- C{c3} from S{b}: 60%" in report
        assert "utm_source" not in report
        assert verify_run(result["run_dir"]) == []

        # An analyst correction supersedes C3 (same source). It
        # must appear as correction lineage, not as a competing
        # claim, and C3 must leave the current evidence.
        correction = correct_claim(
            writer, case_id, c3, "60", "Page 4",
            "Fictional location correction",
        )
        replacement = correction["replacement_claim_id"]

        # A fresh snapshot (test database connection, never the
        # default production connection) sees the later changes.
        reader.start_transaction(
            consistent_snapshot=True,
            isolation_level="REPEATABLE READ",
            readonly=True,
        )
        later = load_evidence_snapshot(
            case_id=case_id, years=(2024, 2025), connection=reader
        )
        reader.rollback()

        later_ids = [c["claim_id"] for c in later["claims"]]
        assert len(later_ids) == 4
        assert c3 not in later_ids and replacement in later_ids
        assert snapshot_sha256(later) != snapshot_sha256(snapshot)

        [lineage] = later["corrections"]
        assert lineage["original_claim_id"] == c3
        assert lineage["replacement_claim_id"] == replacement
        assert lineage["original_location"] == "Page 3"
        assert lineage["replacement_location"] == "Page 4"
        assert lineage["corrected_at_utc"].endswith("Z")

        corrected = generate_versioned_report(
            case_id=case_id,
            output_root=tmp_path,
            snapshot=later,
            run_id="integration-corrected",
            code_version={"git_commit": None, "working_tree_dirty": None},
        )
        corrected_report = (
            corrected["run_dir"] / "report.md"
        ).read_text("utf-8")

        assert (
            f"C{c3}: 60% (Page 3) | C{replacement}: 60% (Page 4)"
            in corrected_report
        )
        # The earlier run is untouched by the later one.
        assert (result["run_dir"] / "report.md").read_text("utf-8") == report
        assert verify_run(corrected["run_dir"]) == []

    finally:
        reader.close()
        try:
            fixtures.cleanup()
        finally:
            writer.close()
