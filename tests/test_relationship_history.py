
"""The read-only comparison rules of src/relationship_history.py (23B.2B-3c). No database."""

from src.gleif_observations import NOT_CAPTURED
from src.relationship_history import CHANGE_NOTE, assessment, compare, stated_events


def entry(source, when, file_id, **values):
    return {"source": source, "data_valid_as_of_utc": when, "source_file_id": file_id,
            "values": values}


def test_consistent_differing_single_and_unknown_fields():
    entries = [entry("jan", "2026-01-01", 1, name="A", status="ACTIVE", category=None, reason=NOT_CAPTURED),
               entry("feb", "2026-02-01", 2, name="B", status="ACTIVE", category=None, reason=NOT_CAPTURED),
               entry("mar", "2026-03-01", 3, name="B", status=None, category="GENERAL", reason=NOT_CAPTURED)]
    result = compare(entries, ("name", "status", "category", "reason", "absent"))

    assert result["name"]["status"] == "DIFFERS"
    assert result["name"]["differences"] == [{
        "from": "jan (data valid as of 2026-01-01)", "to": "feb (data valid as of 2026-02-01)",
        "before": "A", "after": "B", "same_valid_as_of": False}]
    assert result["name"]["note"] == CHANGE_NOTE
    assert (result["status"]["status"], result["status"]["entries_not_stating"]) == ("CONSISTENT", 1)
    assert result["category"]["status"] == "SINGLE_OBSERVATION"
    assert result["reason"]["status"] == "NOT_CAPTURED"
    assert result["absent"]["status"] == "NOT_STATED"


def test_not_captured_legacy_values_are_never_differences():
    entries = [entry("legacy", "2025-01-01", None, status=NOT_CAPTURED, validation="ENTITY_SUPPLIED_ONLY"),
               entry("file", "2026-01-01", 1, status="ACTIVE", validation="ENTITY_SUPPLIED_ONLY")]
    result = compare(entries, ("status", "validation"))
    assert result["status"]["status"] == "SINGLE_OBSERVATION"
    assert result["validation"]["status"] == "CONSISTENT"


def test_same_valid_as_of_differences_are_flagged():
    entries = [entry("a", "2026-01-01", 1, status="ACTIVE"), entry("b", "2026-01-01", 2, status="INACTIVE")]
    (difference,) = compare(entries, ("status",))["status"]["differences"]
    assert difference["same_valid_as_of"] is True


def test_assessment_never_determines_a_corporate_change():
    entries = [entry("a", "2026-01-01", 1, status="ACTIVE"), entry("b", "2026-02-01", 1, status="INACTIVE")]
    changed = assessment(compare(entries, ("status",)), entries)
    same = assessment(compare(entries[:1] * 2, ("status",)), entries[:1] * 2)

    for result in (changed, same):
        assert result["corporate_change_determination"] == "INSUFFICIENT_EVIDENCE"
        assert "not independent corroboration" in result["note"]
    assert changed["differing_fields"] == ["status"] and changed["distinct_source_files"] == 1
    assert "does not establish that nothing changed" in same["note"]


def test_stated_events_are_quoted_not_inferred():
    entries = [{**entry("f", "2026-01-01", 1), "events": [{
        "type": "CHANGE_LEGAL_NAME", "event_status": "IN_PROGRESS",
        "effective": None, "recorded": {"text": "2025-12-01T00:00:00+01:00", "utc": "x"}}]}]
    (event,) = stated_events(entries)
    assert event["effective_as_stated"] is None                 # never invented
    assert event["recorded_as_stated"] == "2025-12-01T00:00:00+01:00"
    assert event["event_status"] == "IN_PROGRESS" and "not verified" in event["note"]
