
"""GLEIF history views (23B.2B-3d): what an investigator sees. No database."""

from pathlib import Path

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

HISTORY = """
import sys
sys.path[:0] = [{root!r}, {root!r} + "/app"]
import gleif_history_ui as ui
from src.gleif_observations import NOT_CAPTURED
from src.relationship_history import (CONSOLIDATION_NOTE, assessment, compare,
                                      RELATIONSHIP_FIELDS, ENTITY_FIELDS)

def rel_entry(origin, when, status, validation, periods):
    values = {{name: None for name in RELATIONSHIP_FIELDS}}
    values.update(relationship_status=status, validation_sources=validation, periods=periods,
                  registration_status=NOT_CAPTURED if origin == "LEGACY_IMPORT" else "PUBLISHED")
    return {{"origin": origin, "source": f"file {{when}}.xml (source file #1)",
             "source_file_id": None if origin == "LEGACY_IMPORT" else 1,
             "data_valid_as_of_utc": when, "values": values, "identifier_flags": None,
             "covering_batches": [] if origin == "LEGACY_IMPORT" else
                 [{{"batch_id": 1, "selection_mode": "FIRST_N", "coverage": "COMPLETE_SAMPLE_OF_FILE"}}]}}

period = [{{"type": "ACCOUNTING_PERIOD", "start": "2023-01-01T00:00:00Z", "end": "2023-12-31T00:00:00Z"}}]
entries = [rel_entry("LEGACY_IMPORT", "2025-10-22T09:00:01.000", NOT_CAPTURED, "ENTITY_SUPPLIED_ONLY", period),
           rel_entry("GLEIF_OBSERVATION", "2026-01-01T00:00:00.000", "ACTIVE", "ENTITY_SUPPLIED_ONLY", period),
           rel_entry("GLEIF_OBSERVATION", "2026-02-01T00:00:00.000", "INACTIVE", "ENTITY_SUPPLIED_ONLY", period)]
comparison = compare(entries, RELATIONSHIP_FIELDS)
relationship = {{"interpretation": CONSOLIDATION_NOTE, "entries": entries, "comparison": comparison,
                 "assessment": assessment(comparison, entries),
                 "absences": [{{"source": "file mar.xml (source file #3)",
                                "data_valid_as_of_utc": "2026-03-01T00:00:00.000",
                                "meaning": "ABSENT_FROM_SAMPLE",
                                "note": "Absence from a file is not evidence that the relationship ended."}}],
                 "audit": {{"observations_only_in_incomplete_batches": 1, "quarantined_records": 2}}}}

def entity(names):
    entity_entries = []
    for when, name in names:
        values = {{field: None for field in ENTITY_FIELDS}}
        values.update(legal_name={{"text": name, "lang": "de"}}, entity_status="ACTIVE",
                      other_entity_names=[{{"text": "Synthetic Alt AG", "type": "PREVIOUS_LEGAL_NAME", "lang": "de"}}])
        entity_entries.append({{"origin": "GLEIF_OBSERVATION", "source": f"file {{when}}.xml",
                                "source_file_id": 1, "data_valid_as_of_utc": when, "values": values,
                                "covering_batches": [], "identifier_flags": [{{"role": "SuccessorEntity[0]"}}]}})
    comparison = compare(entity_entries, ENTITY_FIELDS)
    return {{"entries": entity_entries, "comparison": comparison,
             "assessment": assessment(comparison, entity_entries),
             "stated_events": [{{"source": "file jan.xml", "type": "CHANGE_LEGAL_NAME",
                                 "event_status": "COMPLETED", "effective_as_stated": None,
                                 "note": "Stated by the GLEIF record; not verified by CRIE."}}],
             "audit": {{"observations_only_in_incomplete_batches": 0, "quarantined_records": 0}}}}

child = entity([("2026-01-01T00:00:00.000", "Synthetic Holdings AG"),
                ("2026-02-01T00:00:00.000", "Synthetic Holdings SE")])
empty = {{"entries": [], "comparison": {{}}, "assessment": {{"differing_fields": []}},
          "stated_events": [], "audit": {{"observations_only_in_incomplete_batches": 0,
                                          "quarantined_records": 0}}}}
parents = {{"relationships": []}}
MODE = {mode!r}
if MODE == "fail":
    def boom(*args):
        raise RuntimeError("synthetic outage")
    ui._load_relationship = boom
    ui._load_entities = boom
else:
    ui._load_relationship = lambda connect_db, *key: (relationship, child, empty, parents)
    ui._load_entities = lambda connect_db, leis: {{lei: child for lei in leis}}
case = {{"child_lei": "A" * 20, "parent_lei": "B" * 20, "relationship_type": "IS_DIRECTLY_CONSOLIDATED_BY"}}
if MODE in ("relationship", "fail"):
    ui.render_relationship_history(case, lambda: None)
if MODE in ("names", "fail"):
    ui.render_observed_names(["A" * 20], lambda: None)
"""


def app(mode):
    return AppTest.from_string(HISTORY.format(root=str(ROOT), mode=mode), default_timeout=30).run()


def text(at):
    return " ".join(e.value for kind in ("caption", "info", "warning", "markdown", "error")
                    for e in getattr(at, kind))


def test_relationship_history_shows_provenance_differences_and_limits():
    at = app("relationship")
    assert not at.exception
    body = text(at)

    assert "not case evidence, and not independent corroboration" in body
    assert "not the date of any corporate event" in body
    assert "not evidence of direct ownership or managerial control" in body
    timeline = at.dataframe[0].value
    assert timeline["Origin"].tolist() == ["Legacy import", "GLEIF observation", "GLEIF observation"]
    assert timeline["Relationship status"].tolist() == ["NOT CAPTURED", "ACTIVE", "INACTIVE"]
    assert timeline["Coverage"].tolist() == ["Legacy import", "Complete sample of file",
                                             "Complete sample of file"]
    assert "ACCOUNTING_PERIOD: 2023-01-01T00:00:00Z to 2023-12-31T00:00:00Z" in timeline[
        "Reported periods"].tolist()
    comparison = at.dataframe[1].value.set_index("Stored field")        # stored names kept
    assert comparison.loc["relationship_status", "Compared field"] == "Relationship status"
    assert comparison.loc["relationship_status", "Result"] == \
        "The published records differ (not a verified corporate change)"
    assert "ACTIVE → INACTIVE" in comparison.loc["relationship_status", "Detail"]
    # The legacy NOT CAPTURED value is unknown, not a difference.
    assert comparison.loc["registration_status", "Result"] == "Consistent across the published records"
    assert "does not establish that, or when, a corporate change occurred" in body
    assert "fields compared:" in body and "differ between published records" in body
    assert "ABSENT_FROM_SAMPLE. Absence from a file is not evidence that the relationship ended." in body
    assert "1 observation(s) exist only in incomplete import attempts" in body
    assert "2 quarantined source record(s)" in body
    assert "not verified by CRIE" in body
    assert "No published GLEIF entity observations are recorded for this LEI." in body


def test_observed_names_help_review_but_never_map():
    at = app("names")
    assert not at.exception
    body = text(at)

    names = at.dataframe[0].value
    assert set(names["Name"]) >= {"Synthetic Holdings AG", "Synthetic Holdings SE", "Synthetic Alt AG"}
    assert set(names["Name type"]) == {"LEGAL_NAME", "PREVIOUS_LEGAL_NAME"}
    assert "they never create or change one" in body and "not proof of identity" in body
    assert "legal names of " + "A" * 20 + " differ between records; this is not a verified name change" in body
    assert at.button == [] or len(at.button) == 0                   # nothing to submit


def test_unavailable_history_is_reported_not_hidden():
    at = app("fail")
    assert not at.exception
    errors = [e.value for e in at.error]
    assert any("GLEIF history is unavailable: synthetic outage" in e for e in errors)
    assert any("Observed names are unavailable: synthetic outage" in e for e in errors)


def test_identity_form_offers_names_for_parties_mapped_and_candidate_leis():
    from test_claim_subject_app import app as subject_app, SPP

    at = subject_app("mixed")
    assert not at.exception
    assert at.session_state["names_for"] == sorted(
        {SPP, "315700I4J1M1NKGWWY89", "SYNTHA00000000000047", "SYNTHB00000000000016"})
    assert at.session_state["saved"] == []                          # viewing saves nothing
