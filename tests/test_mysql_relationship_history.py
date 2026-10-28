
"""
Read-only history and comparison (23B.2B-3c) against the real test database
(corporate_intelligence_test only), built from synthetic GLEIF imports.
"""

import datetime

import pytest

from src.gleif_import import import_rr_file
from src.gleif_observations import NOT_CAPTURED, canonical_selection
from src.relationship_history import entity_history, parent_history, relationship_history

from gleif_lei2_samples import event
from gleif_rr_samples import relationship
from test_mysql_gleif_lei2_import import (  # noqa: F401
    A,
    B,
    BAD,
    C,
    block_default_connections,
    connect,
    count,
    db,
    entity,
    lei2,
    pytestmark,
    rows,
    rr,
    run,
)

TYPE = "IS_DIRECTLY_CONSOLIDATED_BY"


def history(function, *args):
    connection = connect()
    try:
        return function(connection, *args)
    finally:
        assert not connection.in_transaction                     # read-only, nothing left open
        connection.close()


def import_rr(path, selection=None, **kwargs):
    return import_rr_file(connect, path, selection or canonical_selection("RR", "ALL_RECORDS"),
                          started_by="synthetic test", **kwargs)


def test_entity_name_change_is_a_difference_not_a_dated_event(db):
    run(lei2(db, "jan", [entity(A, name="Synthetic Holdings AG", category=None,
                                events=(event(effective="2026-01-15T00:00:00+01:00"),))],
             content_date="2026-01-01T00:00:00Z"))
    run(lei2(db, "feb", [entity(A, name="Synthetic Holdings SE", category=None)],
             content_date="2026-02-01T00:00:00Z"))
    result = history(entity_history, A)

    assert [e["data_valid_as_of_utc"] for e in result["entries"]] == [
        "2026-01-01T00:00:00.000", "2026-02-01T00:00:00.000"]
    name = result["comparison"]["legal_name"]
    assert name["status"] == "DIFFERS"
    assert (name["differences"][0]["before"]["text"], name["differences"][0]["after"]["text"]) == (
        "Synthetic Holdings AG", "Synthetic Holdings SE")
    assert result["comparison"]["legal_jurisdiction"]["status"] == "CONSISTENT"
    assert result["comparison"]["entity_category"]["status"] == "NOT_STATED"
    assert result["assessment"]["corporate_change_determination"] == "INSUFFICIENT_EVIDENCE"
    assert result["assessment"]["distinct_source_files"] == 2
    # The record's own event date is quoted as stated; nothing is inferred.
    (stated,) = result["stated_events"]
    assert stated["effective_as_stated"] == "2026-01-15T00:00:00+01:00"
    assert result["identity_row"]["observed_as_of"] == NOT_CAPTURED


def test_unchanged_records_are_consistent_but_prove_no_stability(db):
    for month in ("01", "02"):
        run(lei2(db, f"same-{month}", [entity(A)], content_date=f"2026-{month}-01T00:00:00Z"))
    result = history(entity_history, A)

    assert result["assessment"]["differing_fields"] == []
    assert "does not establish that nothing changed" in result["assessment"]["note"]
    assert "not independent corroboration" in result["assessment"]["note"]


def test_incomplete_batches_and_quarantine_are_audit_only(db):
    def fail(chunk):
        raise RuntimeError("synthetic failure")

    path = lei2(db, "failed", [entity(A), entity(B)])
    with pytest.raises(RuntimeError):
        run(path, chunk_size=1, hooks={"after_commit": lambda chunk: fail(chunk) if chunk == 1 else None})
    run(lei2(db, "quarantine", [entity(BAD)]))

    failed = history(entity_history, A)
    assert failed["entries"] == []
    assert failed["audit"]["observations_only_in_incomplete_batches"] == 1
    held = history(entity_history, BAD)
    assert held["entries"] == [] and held["audit"]["quarantined_records"] == 1
    assert held["identity_row"] is None


def test_relationship_differences_periods_absence_and_interpretation(db):
    periods = (("ACCOUNTING_PERIOD", "2024-01-01T00:00:00Z", "2024-12-31T00:00:00Z"),)
    import_rr(rr(db, "rr-jan", [relationship(A, B, status="ACTIVE", periods=periods)],
                 content_date="2026-01-01T00:00:00Z"))
    import_rr(rr(db, "rr-feb", [relationship(A, B, status="INACTIVE", periods=periods)],
                 content_date="2026-02-01T00:00:00Z"))
    import_rr(rr(db, "rr-mar", [relationship(C, B)], content_date="2026-03-01T00:00:00Z"))
    sample = rr(db, "rr-apr", [relationship(C, B), relationship(A, B)],
                content_date="2026-04-01T00:00:00Z")
    import_rr(sample, canonical_selection("RR", "FIRST_N", limit=1))
    result = history(relationship_history, A, B, TYPE)

    assert result["comparison"]["relationship_status"]["status"] == "DIFFERS"
    assert result["comparison"]["periods"]["status"] == "CONSISTENT"          # as stated
    assert result["comparison"]["periods"]["values"] == [[{
        "type": "ACCOUNTING_PERIOD", "start": "2024-01-01T00:00:00Z", "end": "2024-12-31T00:00:00Z"}]]
    assert "not evidence of direct ownership or managerial control" in result["interpretation"]
    assert [(a["data_valid_as_of_utc"][:10], a["meaning"]) for a in result["absences"]] == [
        ("2026-03-01", "NOT_PUBLISHED_IN_FULL_DATASET"), ("2026-04-01", "ABSENT_FROM_SAMPLE")]
    assert all("not evidence that the relationship ended" in a["note"] for a in result["absences"])


def test_legacy_anchor_values_stay_not_captured(db):
    import_rr(rr(db, "rr-new", [relationship(A, B, validation="ENTITY_SUPPLIED_ONLY")],
                 content_date="2026-05-01T00:00:00Z"))
    connection = connect()
    cursor = connection.cursor()
    cursor.execute("INSERT INTO relationship_evidence (child_lei, parent_lei, relationship_type, "
                   "snapshot_at_utc, validation_sources, validation_documents, periods_json, "
                   "source_file) VALUES (%s, %s, %s, %s, 'ENTITY_SUPPLIED_ONLY', "
                   "'OTHER_OFFICIAL_DOCUMENTS', '[]', 'synthetic-legacy.xml')",
                   (A, B, TYPE, datetime.datetime(2025, 1, 1)))
    connection.commit()
    cursor.close()
    connection.close()
    result = history(relationship_history, A, B, TYPE)

    legacy, observed = result["entries"]
    assert (legacy["origin"], observed["origin"]) == ("LEGACY_IMPORT", "GLEIF_OBSERVATION")
    assert legacy["values"]["relationship_status"] == NOT_CAPTURED
    assert result["comparison"]["relationship_status"]["status"] == "SINGLE_OBSERVATION"
    assert result["comparison"]["validation_sources"]["status"] == "CONSISTENT"


def test_quarantined_relationships_never_become_history(db):
    import_rr(rr(db, "rr-bad", [relationship(BAD, B)]))
    result = history(relationship_history, BAD, B, TYPE)
    assert result["entries"] == [] and result["audit"]["quarantined_records"] == 1


def test_parent_history_lists_each_reported_parent(db):
    import_rr(rr(db, "rr-p1", [relationship(A, B)], content_date="2026-01-01T00:00:00Z"))
    import_rr(rr(db, "rr-p2", [relationship(A, C)], content_date="2026-02-01T00:00:00Z"))
    result = history(parent_history, A)

    parents = {r["parent_lei"]: r for r in result["relationships"]}
    assert set(parents) == {B, C}
    assert [a["meaning"] for a in parents[B]["absent_from"]] == ["NOT_PUBLISHED_IN_FULL_DATASET"]
    assert "not independent corroboration" in result["note"]


def test_history_reads_change_nothing(db):
    run(lei2(db, "ro", [entity(A)]))
    before = {t: count(t) for t in ("legal_entity_observations", "gleif_import_batches",
                                    "legal_entities", "relationship_evidence")}
    history(entity_history, A)
    history(relationship_history, A, B, TYPE)
    history(parent_history, A)
    assert {t: count(t) for t in before} == before


def test_interface_tables_accept_the_real_history_output(db):
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "app"))
    from gleif_history_ui import comparison_rows, entity_rows, name_rows, relationship_rows

    run(lei2(db, "ui-jan", [entity(A, name="Synthetic Holdings AG",
                                   other_names=(("Synthetic Alt AG", "PREVIOUS_LEGAL_NAME", "de"),))],
             content_date="2026-01-01T00:00:00Z"))
    import_rr(rr(db, "ui-rr", [relationship(A, B)], content_date="2026-01-01T00:00:00Z"))
    entity_result = history(entity_history, A)
    relationship_result = history(relationship_history, A, B, TYPE)

    assert [r["Legal name"] for r in entity_rows(entity_result)] == ["Synthetic Holdings AG [de]"]
    assert {(r["Name"], r["Name type"]) for r in name_rows(A, entity_result)} == {
        ("Synthetic Holdings AG", "LEGAL_NAME"), ("Synthetic Alt AG", "PREVIOUS_LEGAL_NAME")}
    (row,) = relationship_rows(relationship_result)
    assert (row["Origin"], row["Coverage"]) == ("GLEIF_OBSERVATION", "COMPLETE_PUBLISHED_DATASET")
    assert row["Data valid as of"] == "2026-01-01T00:00:00.000"
    assert {r["Field"] for r in comparison_rows(relationship_result["comparison"])} >= {
        "relationship_status", "periods"}
