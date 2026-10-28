
import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402


SETUP = """
import sys
from unittest.mock import MagicMock
sys.path[:0] = [{root!r}, {root!r} + "/app"]

import streamlit as st
import claim_correction_ui as ui

MODE = {mode!r}
# Survives st.rerun() after a save, so tests can inspect the calls.
CALLS = st.session_state.setdefault("calls", [])

CLAIM = {{"claim_id": 3, "entity_name": "Fictional plc", "metric": "interest_pct",
          "report_year": 2025, "value_text": "49", "source_location": "Page 3",
          "source_id": 7, "document_title": "Fictional report", "source_url": "https://example.invalid"}}
VERSIONS = [
    {{"claim_id": 1, "value_text": "49", "source_location": "Page 3", "created_at": "2026-01-01"}},
    {{"claim_id": 2, "value_text": "51", "source_location": "Page 4", "created_at": "2026-01-02"}},
    {{"claim_id": 3, "value_text": "52", "source_location": "Page 4", "created_at": "2026-01-03"}},
]
TRANSITIONS = [
    {{"correction_id": 10, "original_claim_id": 1, "replacement_claim_id": 2,
      "correction_kind": "CORRECTION", "restored_claim_id": None,
      "correction_reason": "Misread", "evidence_source_id": 7,
      "evidence_location": "Page 4", "evidence_document_title": "Fictional report",
      "corrected_at_utc": "2026-01-02"}},
    {{"correction_id": 11, "original_claim_id": 2, "replacement_claim_id": 3,
      "correction_kind": "CORRECTION", "restored_claim_id": None,
      "correction_reason": "Restated", "evidence_source_id": None,
      "evidence_location": None, "evidence_document_title": None,
      "corrected_at_utc": "2026-01-03"}},
]

ui._load_claims = lambda connect_db, case_id: [dict(CLAIM, value_text="52", source_location="Page 4")]
ui._load_sources = lambda connect_db, case_id: [
    {{"source_id": 7, "document_title": "Fictional report"}},
    {{"source_id": 8, "document_title": "Fictional filing"}},
]
ui._load_lineage = lambda connect_db, case_id, claim_id: (
    (False, None) if MODE == "legacy" else
    (True, {{"versions": VERSIONS, "transitions": TRANSITIONS,
             "active_claim_id": 3, "root_claim_id": 1, "problems": []}})
)
ui._load_history = lambda connect_db, case_id: []

def diagnostics(status, schema="INSTALLED", problems=()):
    return {{"status": status, "lineages": 1, "corrections": 2,
             "attribution_not_recorded": 1, "problems": list(problems),
             "missing_claims": [], "lineage_schema": schema}}

DIAGNOSTICS = {{
    "legacy": diagnostics("NO_PROBLEMS_FOUND", schema="NOT_INSTALLED"),
    "installed": diagnostics("NO_PROBLEMS_FOUND"),
    "problems": diagnostics("PROBLEMS_FOUND", problems=["claim 1 has more than one replacement"]),
    "incomplete": diagnostics("INCOMPLETE", problems=["claim 9 is referenced by a correction but is not available"]),
}}
ui._load_diagnostics = lambda connect_db, case_id: DIAGNOSTICS[MODE]

def record(name):
    def action(**kwargs):
        CALLS.append((name, kwargs))
        return {{"original_claim_id": 3, "replacement_claim_id": 4}}
    return action

ui.correct_claim = record("correct")
ui.restore_claim = record("restore")

ui.render_claim_correction_ui(1, lambda: MagicMock())
"""


def app(mode):
    from pathlib import Path

    root = str(Path(__file__).resolve().parents[1])
    return AppTest.from_string(
        SETUP.format(root=root, mode=mode), default_timeout=30
    ).run()


def test_legacy_schema_pauses_corrections():
    at = app("legacy")

    assert not at.exception
    assert "migration (Milestone 22.4A) is not installed" in at.info[0].value
    assert len(at.button) == 0


def test_versions_and_transitions_displayed():
    at = app("installed")

    assert not at.exception
    versions, transitions = at.dataframe[0].value, at.dataframe[1].value
    assert versions["Status"].tolist() == ["Superseded", "Superseded", "Active"]
    assert versions["Value"].tolist() == ["49", "51", "52"]
    assert transitions["Evidence"].tolist() == [
        "Fictional report — Page 4", "Not recorded",
    ]
    assert transitions["Reason"].tolist() == ["Misread", "Restated"]
    # Correcting and restoring are distinct choices.
    assert at.radio[0].options == ["Correct the value or location",
                                   "Restore an earlier version"]


def test_correction_submits_evidence():
    at = app("installed")
    at.text_input[0].set_value("53")
    at.text_input[2].set_value("Note 9")
    at.selectbox[1].set_value(8)
    at.text_area[0].set_value("Fictional restatement")
    at.checkbox[0].check()
    at.button[0].click().run()

    assert not at.exception
    assert not at.error
    assert at.session_state["calls"] == [("correct", {
        "connection": at.session_state["calls"][0][1]["connection"],
        "case_id": 1, "original_claim_id": 3, "new_value": "53",
        "new_location": "Page 4", "reason": "Fictional restatement",
        "evidence_source_id": 8, "evidence_location": "Note 9",
    })]


def test_restore_is_a_separate_action():
    at = app("installed")
    at.radio[0].set_value("Restore an earlier version").run()

    assert not at.exception
    labels = [s.label for s in at.selectbox]
    assert "Earlier version to restore" in labels
    restore = at.selectbox[labels.index("Earlier version to restore")]
    assert restore.options == ["Claim #1: 49 (Page 3)", "Claim #2: 51 (Page 4)"]
    assert any("creates a new claim" in c.value for c in at.caption)

    restore.set_value(1)
    at.text_input[0].set_value("Page 3")
    at.text_area[0].set_value("Fictional: original confirmed")
    at.checkbox[0].check()
    at.button[0].click().run()

    assert not at.exception
    (name, arguments), = at.session_state["calls"]
    assert name == "restore"
    assert {k: arguments[k] for k in ("current_claim_id", "restored_claim_id",
                                      "evidence_source_id", "evidence_location")} == {
        "current_claim_id": 3, "restored_claim_id": 1,
        "evidence_source_id": 7, "evidence_location": "Page 3",
    }


def test_unverified_submission_is_refused():
    at = app("installed")
    at.text_area[0].set_value("Fictional reason")
    at.button[0].click().run()

    assert not at.exception
    assert "Verify the source before saving." in at.error[0].value


def test_diagnostics_without_lineage_schema():
    at = app("legacy")

    assert not at.exception
    assert any(e.label == "Lineage diagnostics" for e in at.expander)
    assert any("does not record correction-lineage metadata" in i.value for i in at.info)
    assert "No lineage problems found (1 lineage(s), 2 correction(s))." in at.success[0].value


def test_diagnostics_report_unrecorded_evidence():
    at = app("installed")

    assert not at.exception
    assert any("Supporting evidence was not recorded for 1 of 2" in c.value
               for c in at.caption)


def test_confirmed_problems_and_incomplete_data_are_distinct():
    problems = app("problems")
    assert "Lineage problems found" in problems.error[0].value
    assert any("more than one replacement" in m.value for m in problems.markdown)

    incomplete = app("incomplete")
    assert not incomplete.error
    assert "Incomplete: referenced claims are unavailable" in incomplete.warning[0].value
    assert not incomplete.success
