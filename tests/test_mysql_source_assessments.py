
"""
Source-relationship assessments against the real test database
(corporate_intelligence_test only). Every record is synthetic. Raw
SQL is used deliberately to show the database itself enforces each
rule, not just the application.
"""

import threading
import uuid

import pytest
from mysql.connector import errors

from src.evidence_snapshot import as_version
from src.report_registry import register_run
from src.report_renderer import analyze_snapshot
from src.source_assessments import AssessmentError, record_assessment

from test_mysql_evidence_snapshot import (  # noqa: F401
    block_default_connections,
    connect,
    pytestmark,
)
from test_mysql_report_registry import (  # noqa: F401
    currency,
    new_run,
    registry_db,
    seed,
    snapshot_now,
)

YEAR, PERIOD = 2025, "2025-12-31"
ENTITY, METRIC = "Fictional Harbour Holdings plc", "harbour_interest_pct"
DISCLOSURE = {"entity_name": ENTITY, "metric": METRIC, "report_year": YEAR, "period_end": PERIOD}


@pytest.fixture
def assessed_db(registry_db):
    connection, fixtures, run_ids = registry_db
    cursor = connection.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = 'source_relationship_assessments'")
    installed = cursor.fetchone()[0]
    cursor.close()
    connection.rollback()
    if not installed:
        pytest.skip("Source-relationship assessments are not installed in the test database; "
                    "apply sql/test_schema_upgrade_m23b1.sql with a privileged account "
                    "(test database only).")

    case_id, a, b, _, c2 = seed(fixtures)
    fixtures.claim(a, YEAR, "49")  # a and b now agree on one disclosure
    connection.commit()
    extra = {"cases": [], "sources": []}
    try:
        yield connection, fixtures, run_ids, case_id, a, b, extra
    finally:
        connection.rollback()
        cleanup = connection.cursor()
        cases = [case_id, *extra["cases"]]
        slots = ", ".join(["%s"] * len(cases))
        cleanup.execute("SET @crie_assessment_maintenance = 1")
        cleanup.execute(f"DELETE FROM source_relationship_assessments WHERE case_id IN ({slots}) "
                        "ORDER BY assessment_id DESC", cases)
        cleanup.execute("SET @crie_assessment_maintenance = 0")
        for source_id in extra["sources"]:
            cleanup.execute("DELETE FROM disclosure_claims WHERE source_id = %s", (source_id,))
            cleanup.execute("DELETE FROM case_sources WHERE source_id = %s", (source_id,))
        for other in extra["cases"]:
            cleanup.execute("DELETE FROM investigation_cases WHERE case_id = %s", (other,))
        connection.commit()
        cleanup.close()


def other_investigation(connection, extra, *titles):
    """A second synthetic case with its own registrations (cleaned up above)."""
    cursor = connection.cursor()
    cursor.execute("INSERT INTO investigation_cases (case_name) VALUES ('Synthetic other case')")
    case_id = cursor.lastrowid
    extra["cases"].append(case_id)
    sources = []
    for title in titles:
        cursor.execute("INSERT INTO case_sources (case_id, document_title) VALUES (%s, %s)",
                       (case_id, title))
        sources.append(cursor.lastrowid)
        extra["sources"].append(cursor.lastrowid)
    connection.commit()
    cursor.close()
    return case_id, sources


def independent(a, b, **overrides):
    fields = dict(source_a_id=a, source_b_id=b, relationship="INDEPENDENT_ORIGIN",
                  basis="DOCUMENTED", rationale="Synthetic: separate fictional filings",
                  recorded_by="synthetic reviewer", limitations="Synthetic fixture only",
                  evidence_reference="Synthetic register 1", evidence_location="Entry 1",
                  disclosure=DISCLOSURE)
    fields.update(overrides)
    return fields


def raw_insert(connection, case_id, a, b, relationship="UNKNOWN", supersedes=None, **columns):
    values = {"case_id": case_id, "source_a_id": a, "source_b_id": b,
              "relationship": relationship, "basis": "INVESTIGATOR_JUDGEMENT",
              "rationale": "Synthetic raw row", "recorded_by": "synthetic",
              "supersedes_assessment_id": supersedes,
              "submission_key": uuid.uuid4().hex * 2, **columns}
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"INSERT INTO source_relationship_assessments ({', '.join(values)}) "
            f"VALUES ({', '.join(['%s'] * len(values))})", tuple(values.values()))
        connection.commit()
        return cursor.lastrowid
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def status_now(case_id):
    analysis = analyze_snapshot(snapshot_now(case_id), 2024, 2025)
    (row,) = [r for r in analysis["reconciliation"] if r["report_year"] == str(YEAR)]
    return row["reconciliation_status"], row["corroborated"]


def test_recorded_independence_flows_into_reconciliation(assessed_db):
    connection, _, _, case_id, a, b, _ = assessed_db
    assert status_now(case_id) == ("INDEPENDENCE_UNVERIFIED", False)

    first = record_assessment(connection, case_id, **independent(a, b))
    assert status_now(case_id) == ("CONSISTENT_SOURCES", True)

    # Withdrawal: supersede with UNKNOWN; history is kept.
    record_assessment(connection, case_id, **independent(
        a, b, relationship="UNKNOWN", rationale="Synthetic: basis withdrawn",
        evidence_reference=None, evidence_location=None, limitations=None,
        supersedes_assessment_id=first["assessment_id"]))
    assert status_now(case_id) == ("INDEPENDENCE_UNVERIFIED", False)
    assert len(snapshot_now(case_id)["source_assessments"]) == 2


def test_idempotent_resubmission(assessed_db):
    connection, _, _, case_id, a, b, _ = assessed_db

    first = record_assessment(connection, case_id, **independent(a, b))
    again = record_assessment(connection, case_id, **independent(b, a))

    assert again == {"assessment_id": first["assessment_id"], "created": False}


@pytest.mark.parametrize("swap, relationship, columns", [
    (True, "UNKNOWN", {}),                          # non-canonical order
    (False, "UNKNOWN", {"metric": METRIC}),         # incomplete scope
    (False, "INDEPENDENT_ORIGIN", {}),              # independence without evidence
    (False, "UNKNOWN", {"rationale": "   "}),       # empty rationale
])
def test_database_check_constraints(assessed_db, swap, relationship, columns):
    connection, _, _, case_id, a, b, _ = assessed_db
    low, high = min(a, b), max(a, b)
    first, second = (high, low) if swap else (low, high)

    with pytest.raises(errors.DatabaseError) as raised:
        raw_insert(connection, case_id, first, second, relationship=relationship, **columns)
    assert raised.value.errno == 3819


def test_cross_case_sources_rejected_by_foreign_keys(assessed_db):
    connection, _, _, case_id, a, b, extra = assessed_db
    _, (other_source,) = other_investigation(connection, extra, "Fictional other report")

    with pytest.raises(errors.DatabaseError) as raised:
        raw_insert(connection, case_id, min(a, other_source), max(a, other_source))
    assert raised.value.errno == 1452
    with pytest.raises(errors.DatabaseError) as raised:
        raw_insert(connection, case_id, min(a, b), max(a, b), evidence_source_id=other_source)
    assert raised.value.errno == 1452
    with pytest.raises(AssessmentError, match="registered in this investigation"):
        record_assessment(connection, case_id, **independent(a, other_source))


def test_one_chain_per_scope_and_same_scope_supersession(assessed_db):
    connection, _, _, case_id, a, b, _ = assessed_db
    a, b = min(a, b), max(a, b)
    root = raw_insert(connection, case_id, a, b)

    # A second root for the same scope.
    with pytest.raises(errors.DatabaseError) as raised:
        raw_insert(connection, case_id, a, b)
    assert raised.value.errno == 1062

    # Superseding from another scope is refused by the trigger.
    with pytest.raises(errors.DatabaseError) as raised:
        raw_insert(connection, case_id, a, b, supersedes=root, entity_name=ENTITY,
                   metric=METRIC, report_year=YEAR, period_end=PERIOD)
    assert raised.value.errno == 1644

    successor = raw_insert(connection, case_id, a, b, supersedes=root)
    with pytest.raises(errors.DatabaseError) as raised:
        raw_insert(connection, case_id, a, b, supersedes=root)
    assert raised.value.errno == 1062
    assert successor > root


def test_append_only(assessed_db):
    connection, _, _, case_id, a, b, _ = assessed_db
    assessment_id = raw_insert(connection, case_id, min(a, b), max(a, b))
    cursor = connection.cursor()
    try:
        for statement in ("UPDATE source_relationship_assessments SET rationale = 'x' "
                          "WHERE assessment_id = %s",
                          "DELETE FROM source_relationship_assessments WHERE assessment_id = %s"):
            with pytest.raises(errors.DatabaseError) as raised:
                cursor.execute(statement, (assessment_id,))
            assert raised.value.errno == 1644
            connection.rollback()
    finally:
        cursor.close()


def test_concurrent_supersession_has_one_winner(assessed_db):
    connection, _, _, case_id, a, b, _ = assessed_db
    first = record_assessment(connection, case_id, **independent(
        a, b, relationship="UNKNOWN", evidence_reference=None, evidence_location=None,
        limitations=None))["assessment_id"]
    barrier, outcomes = threading.Barrier(2), []

    def attempt(rationale):
        own = connect()
        try:
            barrier.wait()
            outcomes.append(record_assessment(own, case_id, **independent(
                a, b, rationale=rationale, supersedes_assessment_id=first)))
        except AssessmentError as error:
            outcomes.append(error)
        finally:
            own.close()

    threads = [threading.Thread(target=attempt, args=(f"Synthetic view {i}",)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert sum(isinstance(o, dict) for o in outcomes) == 1
    assert sum(isinstance(o, AssessmentError) for o in outcomes) == 1


def test_assessments_stay_in_their_investigation(assessed_db):
    connection, fixtures, _, case_id, a, b, extra = assessed_db
    record_assessment(connection, case_id, **independent(a, b))
    # The same fictional documents registered in a second investigation.
    other_case, (copy_a, copy_b) = other_investigation(
        connection, extra, "Fictional report 1", "Fictional report 2")
    fixtures.claim(copy_a, YEAR, "49")
    fixtures.claim(copy_b, YEAR, "49")
    connection.commit()

    other = snapshot_now(other_case)
    assert other["source_assessments"] == []
    assert status_now(other_case) == ("INDEPENDENCE_UNVERIFIED", False)
    assert status_now(case_id) == ("CONSISTENT_SOURCES", True)


def test_version_3_runs_go_stale_while_version_2_runs_stay_current(assessed_db, tmp_path):
    connection, _, run_ids, case_id, a, b, _ = assessed_db
    current = snapshot_now(case_id)
    run_ids += ["synthetic-assess-v3", "synthetic-assess-v2"]
    new_run(tmp_path, case_id, "synthetic-assess-v3", snapshot=current)
    new_run(tmp_path, case_id, "synthetic-assess-v2", snapshot=as_version(current, "2"))
    for run_id in run_ids:
        register_run(connection, case_id, run_id, tmp_path)

    record_assessment(connection, case_id, **independent(a, b))

    v3 = currency(case_id, "synthetic-assess-v3", tmp_path)
    v2 = currency(case_id, "synthetic-assess-v2", tmp_path)
    assert (v3["integrity"]["status"], v3["currency"]["status"]) == ("INTACT", "STALE")
    assert (v2["integrity"]["status"], v2["currency"]["status"]) == ("INTACT", "CURRENT")


def test_case_variant_scope_is_a_different_scope(assessed_db):
    connection, _, _, case_id, a, b, _ = assessed_db
    low, high = min(a, b), max(a, b)
    scoped = dict(entity_name=ENTITY, metric=METRIC, report_year=YEAR, period_end=PERIOD)
    root = raw_insert(connection, case_id, low, high, **scoped)

    # The database refuses to link it to a case-variant scope...
    for variant in (ENTITY.upper(), ENTITY + " "):
        with pytest.raises(errors.DatabaseError) as raised:
            raw_insert(connection, case_id, low, high, supersedes=root,
                       **{**scoped, "entity_name": variant})
        assert raised.value.errno == 1644

    # ...and the application does not treat it as the same disclosure.
    with pytest.raises(AssessmentError, match="Neither source has a claim"):
        record_assessment(connection, case_id, **independent(
            a, b, disclosure={**DISCLOSURE, "entity_name": ENTITY.upper()}))
