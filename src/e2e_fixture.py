
"""
Faithful end-to-end fixture of one real investigation case.

    python -m src.e2e_fixture extract --case-id 1 --output <file.json>

extract() reads, from production and with SELECT only, the minimum
records the evidence snapshot and the relationship context need,
inside one read-only consistent UTC transaction. Investigator case
notes are not included. The output file is written once (never
overwritten) with owner-only permissions; keep it outside Git.

load() inserts those records, with their original identifiers and
timestamps, into the disposable end-to-end database only, after
checking that none of the identifiers is already in use. remove()
deletes exactly the loaded rows, and nothing else.
"""

import argparse
import base64
import datetime
import decimal
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if not __package__:
    sys.path.insert(0, str(ROOT))

PRODUCTION_DATABASE = "corporate_intelligence"
E2E_DATABASE = "corporate_intelligence_e2e"
FIXTURE_VERSION = 1

# (table, key columns, query) in foreign-key order. Every query is
# restricted to the case and its own relationship.
TABLES = [
    ("legal_entities", ("lei",),
     "SELECT e.* FROM legal_entities e JOIN investigation_cases c "
     "ON e.lei IN (c.child_lei, c.parent_lei) WHERE c.case_id = %s ORDER BY e.lei"),
    ("corporate_relationships", ("child_lei", "parent_lei", "relationship_type"),
     "SELECT r.* FROM corporate_relationships r JOIN investigation_cases c "
     "USING (child_lei, parent_lei, relationship_type) WHERE c.case_id = %s"),
    ("relationship_evidence", ("child_lei", "parent_lei", "relationship_type", "snapshot_at_utc"),
     "SELECT e.* FROM relationship_evidence e JOIN investigation_cases c "
     "USING (child_lei, parent_lei, relationship_type, snapshot_at_utc) WHERE c.case_id = %s"),
    ("relationship_periods",
     ("child_lei", "parent_lei", "relationship_type", "snapshot_at_utc", "period_index"),
     "SELECT p.* FROM relationship_periods p JOIN investigation_cases c "
     "USING (child_lei, parent_lei, relationship_type, snapshot_at_utc) "
     "WHERE c.case_id = %s ORDER BY p.period_index"),
    ("investigation_cases", ("case_id",),
     "SELECT * FROM investigation_cases WHERE case_id = %s"),
    ("case_sources", ("source_id",),
     "SELECT * FROM case_sources WHERE case_id = %s ORDER BY source_id"),
    ("source_assessment_history", ("history_id",),
     "SELECT h.* FROM source_assessment_history h JOIN case_sources s "
     "ON s.source_id = h.source_id WHERE s.case_id = %s ORDER BY h.history_id"),
    ("source_history_annotations", ("annotation_id",),
     "SELECT a.* FROM source_history_annotations a JOIN source_assessment_history h "
     "ON h.history_id = a.history_id JOIN case_sources s ON s.source_id = h.source_id "
     "WHERE s.case_id = %s ORDER BY a.annotation_id"),
    ("source_metadata_history", ("change_id",),
     "SELECT m.* FROM source_metadata_history m JOIN case_sources s "
     "ON s.source_id = m.source_id WHERE s.case_id = %s ORDER BY m.change_id"),
    ("disclosure_claims", ("claim_id",),
     "SELECT c.* FROM disclosure_claims c JOIN case_sources s "
     "ON s.source_id = c.source_id WHERE s.case_id = %s ORDER BY c.claim_id"),
    ("claim_corrections", ("correction_id",),
     "SELECT cc.* FROM claim_corrections cc JOIN disclosure_claims o "
     "ON o.claim_id = cc.original_claim_id JOIN case_sources s "
     "ON s.source_id = o.source_id WHERE s.case_id = %s ORDER BY cc.correction_id"),
    # Milestone 23B.1; absent (and so empty) where not installed.
    ("source_relationship_assessments", ("assessment_id",),
     "SELECT assessment_id, case_id, source_a_id, source_b_id, entity_name, metric, "
     "report_year, period_end, relationship, basis, evidence_source_id, "
     "evidence_reference, evidence_location, rationale, limitations, recorded_by, "
     "recorded_at_utc, supersedes_assessment_id, submission_key "
     "FROM source_relationship_assessments WHERE case_id = %s ORDER BY assessment_id"),
    # Milestone 23B.2A; absent (and so empty) where not installed.
    ("claim_subject_mappings", ("mapping_id",),
     "SELECT mapping_id, case_id, subject_text, status, lei, name_type, candidate_leis, "
     "basis, evidence_source_id, evidence_reference, evidence_location, rationale, "
     "limitations, recorded_by, recorded_at_utc, supersedes_mapping_id, submission_key "
     "FROM claim_subject_mappings WHERE case_id = %s ORDER BY mapping_id"),
]

OPTIONAL_TABLES = {"source_relationship_assessments", "claim_subject_mappings"}


class FixtureError(Exception):
    """The fixture cannot be extracted or loaded safely."""


def _encode(value):
    if isinstance(value, datetime.datetime):
        return {"datetime": value.isoformat()}
    if isinstance(value, datetime.date):
        return {"date": value.isoformat()}
    if isinstance(value, decimal.Decimal):
        return {"decimal": str(value)}
    if isinstance(value, (bytes, bytearray)):
        return {"bytes": base64.b64encode(bytes(value)).decode("ascii")}
    return value


def _decode(value):
    if isinstance(value, dict) and len(value) == 1:
        (kind, text), = value.items()
        if kind == "datetime":
            return datetime.datetime.fromisoformat(text)
        if kind == "date":
            return datetime.date.fromisoformat(text)
        if kind == "decimal":
            return decimal.Decimal(text)
        if kind == "bytes":
            return base64.b64decode(text)
    return value


def require_database(connection, expected):
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT DATABASE()")
        actual = cursor.fetchone()[0]
    finally:
        cursor.close()
    if actual != expected:
        raise FixtureError(f"Connected to {actual!r}, expected {expected!r}")


def extract(connection, case_id):
    """
    Read one case from production (SELECT only). The caller owns the
    connection; it must have no open transaction.
    """

    require_database(connection, PRODUCTION_DATABASE)
    connection.rollback()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SET SESSION time_zone = '+00:00'")
        connection.start_transaction(
            consistent_snapshot=True, isolation_level="REPEATABLE READ", readonly=True,
        )
        tables = {}
        for table, _, query in TABLES:
            if table in OPTIONAL_TABLES:
                cursor.execute(
                    "SELECT COUNT(*) AS n FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (table,))
                if not cursor.fetchone()["n"]:
                    tables[table] = []
                    continue
            cursor.execute(query, (case_id,))
            tables[table] = [{k: _encode(v) for k, v in row.items()}
                             for row in cursor.fetchall()]
    finally:
        cursor.close()
        connection.rollback()

    if len(tables["investigation_cases"]) != 1:
        raise FixtureError(f"Investigation case {case_id} not found")

    return {"fixture_version": FIXTURE_VERSION, "case_id": int(case_id),
            "source_database": PRODUCTION_DATABASE, "tables": tables}


def write_fixture(fixture, path):
    """Write once, owner-only; never overwrite."""

    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = json.dumps(fixture, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


def read_fixture(path):
    fixture = json.loads(Path(path).read_text("utf-8"))
    if fixture.get("fixture_version") != FIXTURE_VERSION:
        raise FixtureError("Unsupported fixture version")
    return fixture


def _rows(fixture, table):
    return [{k: _decode(v) for k, v in row.items()}
            for row in fixture["tables"].get(table, [])]


def _where(keys):
    return " AND ".join(f"`{k}` = %s" for k in keys)


def load(connection, fixture):
    """
    Insert the fixture into the end-to-end database in one
    transaction, refusing if any identifier is already present.
    The caller owns the connection; it must have no open transaction.
    """

    require_database(connection, E2E_DATABASE)
    connection.rollback()
    cursor = connection.cursor()
    try:
        cursor.execute("SET SESSION time_zone = '+00:00'")
        connection.start_transaction()
        for table, keys, _ in TABLES:
            for row in _rows(fixture, table):
                cursor.execute(
                    f"SELECT COUNT(*) FROM `{table}` WHERE {_where(keys)} FOR UPDATE",
                    tuple(row[k] for k in keys),
                )
                if cursor.fetchone()[0]:
                    raise FixtureError(f"{table} already holds a row with the fixture's key")
                columns = ", ".join(f"`{c}`" for c in row)
                slots = ", ".join(["%s"] * len(row))
                cursor.execute(f"INSERT INTO `{table}` ({columns}) VALUES ({slots})",
                               tuple(row.values()))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def remove(connection, fixture):
    """Delete exactly the fixture's rows, in reverse foreign-key order."""

    require_database(connection, E2E_DATABASE)
    connection.rollback()
    cursor = connection.cursor()
    try:
        cursor.execute("SET SESSION time_zone = '+00:00'")
        cursor.execute("SET @crie_claim_maintenance = 1")
        cursor.execute("SET @crie_assessment_maintenance = 1")
        cursor.execute("SET @crie_mapping_maintenance = 1")
        for table, keys, _ in reversed(TABLES):
            for row in _rows(fixture, table):
                cursor.execute(f"DELETE FROM `{table}` WHERE {_where(keys)}",
                               tuple(row[k] for k in keys))
        cursor.execute("SET @crie_claim_maintenance = 0")
        cursor.execute("SET @crie_assessment_maintenance = 0")
        cursor.execute("SET @crie_mapping_maintenance = 0")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m src.e2e_fixture")
    parser.add_argument("action", choices=["extract"])
    parser.add_argument("--case-id", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    if (ROOT / ".git").exists() and args.output.resolve().is_relative_to(ROOT) \
            and "private" not in args.output.resolve().relative_to(ROOT).parts:
        print("Refused: inside the repository the fixture must go under a "
              "'private' directory, which Git ignores.", file=sys.stderr)
        return 2

    import mysql.connector
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    connection = mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"), port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"), password=os.getenv("MYSQL_PASSWORD"),
        database=PRODUCTION_DATABASE,
    )
    try:
        fixture = extract(connection, args.case_id)
    finally:
        connection.close()

    write_fixture(fixture, args.output)
    counts = ", ".join(f"{t}: {len(r)}" for t, r in fixture["tables"].items())
    print(f"Fixture written ({counts})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
