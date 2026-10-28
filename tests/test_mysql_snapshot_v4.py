
"""
Snapshot version 4 against the real test database (corporate_intelligence_test
only): a synthetic case, synthetic LEI2 imports and recorded identity mappings.
"""

import pytest

from src.claim_subjects import record_mapping
from src.evidence_snapshot import as_version, load_evidence_snapshot, snapshot_sha256
from src.generate_versioned_report import generate_versioned_report, verify_run

from test_mysql_gleif_lei2_import import (  # noqa: F401
    A,
    B,
    BAD,
    block_default_connections,
    connect,
    db,
    entity,
    lei2,
    pytestmark,
    run,
)
from test_mysql_report_registry import registry_db, seed  # noqa: F401


def snapshot(case_id, version):
    connection = connect()
    try:
        connection.start_transaction(consistent_snapshot=True, isolation_level="REPEATABLE READ",
                                     readonly=True)
        try:
            return load_evidence_snapshot(case_id=case_id, years=(2024, 2025),
                                          connection=connection, version=version)
        finally:
            connection.rollback()
    finally:
        connection.close()


@pytest.fixture
def case(db, registry_db):
    _, fixtures, _ = registry_db
    case_id, *_ = seed(fixtures)
    try:
        yield case_id
    finally:
        connection = connect()
        cursor = connection.cursor()
        cursor.execute("SET @crie_mapping_maintenance = 1")
        cursor.execute("DELETE FROM claim_subject_mappings WHERE case_id = %s "
                       "ORDER BY mapping_id DESC", (case_id,))
        cursor.execute("SET @crie_mapping_maintenance = 0")
        connection.commit()
        cursor.close()
        connection.close()


def subject(case_id):
    return snapshot(case_id, "3")["claims"][0]["entity"]


def map_subject(case_id, **fields):
    connection = connect()
    try:
        return record_mapping(connection, case_id, recorded_by="synthetic reviewer",
                              basis="DOCUMENTED", rationale="Synthetic rationale",
                              evidence_reference="Synthetic register",
                              evidence_location="Entry 1", **fields)
    finally:
        connection.close()


def test_version_4_reads_identity_and_context_and_projects_byte_for_byte(db, case):
    run(lei2(db, "v4", [entity(A), entity(B), entity(BAD)]))
    first = map_subject(case, subject_text=subject(case), status="AMBIGUOUS",
                        candidate_leis=[A, B])
    map_subject(case, subject_text=subject(case), status="MAPPED", lei=A, name_type="LEGAL_NAME",
                limitations="Synthetic only", supersedes_mapping_id=first["mapping_id"])

    four = snapshot(case, "4")
    assert four["snapshot_schema_version"] == "4"
    identity = four["case_identity"]
    assert [m["status"] for m in identity["mappings"]] == ["AMBIGUOUS", "MAPPED"]
    assert identity["mappings"][0]["candidate_leis"] == [A, B]
    context = four["gleif_context"]
    assert [e["subject"]["lei"] for e in context["entities"]] == [A, B]       # named by the case
    assert context["relationship"] is None                                   # no examined relationship
    assert all(e["assessment"]["corporate_change_determination"] == "INSUFFICIENT_EVIDENCE"
               for e in context["entities"])
    assert BAD not in str(context)                                           # quarantined: never context

    # Byte-level compatibility: versions 1-3 equal direct reads of the same evidence.
    for version in ("1", "2", "3"):
        assert snapshot_sha256(as_version(four, version)) == snapshot_sha256(snapshot(case, version))
    # Deterministic.
    assert snapshot_sha256(snapshot(case, "4")) == snapshot_sha256(four)


def test_mapping_changes_version_4_only(db, case):
    run(lei2(db, "v4b", [entity(A)]))
    before = {v: snapshot_sha256(snapshot(case, v)) for v in ("1", "2", "3", "4")}
    map_subject(case, subject_text=subject(case), status="UNRESOLVED")
    after = {v: snapshot_sha256(snapshot(case, v)) for v in ("1", "2", "3", "4")}

    assert {v: after[v] == before[v] for v in after} == {"1": True, "2": True, "3": True, "4": False}


def test_version_4_runs_are_explicit_and_verifiable(db, case, tmp_path):
    run(lei2(db, "v4c", [entity(A)]))
    four = snapshot(case, "4")
    result = generate_versioned_report(case_id=case, output_root=tmp_path, snapshot=four,
                                       code_version={"git_commit": None, "working_tree_dirty": None})
    assert verify_run(result["run_dir"]) == []
    assert "### I. Case Identity and Global GLEIF Context" in (result["run_dir"] / "report.md").read_text()
    default = generate_versioned_report(case_id=case, output_root=tmp_path,
                                        snapshot=snapshot(case, "3"),
                                        code_version={"git_commit": None, "working_tree_dirty": None})
    assert "### I." not in (default["run_dir"] / "report.md").read_text()
