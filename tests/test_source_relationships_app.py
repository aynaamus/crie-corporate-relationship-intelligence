
import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402


SETUP = """
import sys, datetime
from unittest.mock import MagicMock
sys.path[:0] = [{root!r}, {root!r} + "/app"]
import streamlit as st
import source_relationships_ui as ui

MODE = {mode!r}
SAVED = st.session_state.setdefault("saved", [])

def row(i, relationship, supersedes=None, scoped=False):
    return {{"assessment_id": i, "source_a_id": 1, "source_b_id": 2,
             "entity_name": "Synthetic plc" if scoped else None,
             "metric": "interest_pct" if scoped else None,
             "report_year": 2025 if scoped else None,
             "period_end": "2025-12-31" if scoped else None,
             "relationship": relationship, "basis": "DOCUMENTED",
             "evidence_source_id": None, "evidence_reference": "Synthetic ref",
             "evidence_location": "p. 1", "rationale": "Synthetic rationale",
             "limitations": None, "recorded_by": "synthetic reviewer",
             "recorded_at_utc": "2026-02-01", "supersedes_assessment_id": supersedes}}

ROWS = {{
    "absent": None,
    "empty": [],
    "history": [row(1, "UNKNOWN"), row(2, "SAME_CORPORATE_GROUP", supersedes=1)],
    "conflict": [row(1, "SAME_CORPORATE_GROUP"), row(2, "INDEPENDENT_ORIGIN", scoped=True)],
}}[MODE]
SOURCES = [{{"source_id": 1, "document_title": "Synthetic A"}},
           {{"source_id": 2, "document_title": "Synthetic B"}}]
DISCLOSURES = [{{"source_id": 1, "entity_name": "Synthetic plc", "metric": "interest_pct",
                 "report_year": 2025, "period_end": datetime.date(2025, 12, 31)}}]

ui._load = lambda connect_db, case_id: (ROWS, SOURCES, DISCLOSURES)

def save(connect_db, case_id, fields):
    SAVED.append(fields)
    return {{"assessment_id": 3, "created": True}}

ui._save = save
ui.render_source_relationships(1, lambda: MagicMock())
"""


def app(mode):
    from pathlib import Path

    root = str(Path(__file__).resolve().parents[1])
    return AppTest.from_string(SETUP.format(root=root, mode=mode), default_timeout=30).run()


def test_missing_schema_is_explained():
    at = app("absent")

    assert not at.exception
    assert "does not record source-relationship assessments" in at.info[0].value
    assert len(at.button) == 0


def test_history_marks_current_and_superseded():
    at = app("history")

    assert not at.exception
    table = at.dataframe[0].value
    assert table["Status"].tolist() == ["Superseded", "Current"]
    assert table["Supersedes"].tolist() == ["—", "#1"]


def test_conflicting_current_assessments_warned():
    at = app("conflict")

    assert not at.exception
    assert "disagree about independence" in at.warning[0].value


def test_new_assessment_recorded_with_its_fields():
    at = app("empty")
    at.selectbox[2].set_value("UNKNOWN")
    at.text_area[0].set_value("Synthetic: no basis to judge")
    at.text_input[2].set_value("synthetic reviewer")
    at.button[0].click().run()

    assert not at.exception
    (fields,) = at.session_state["saved"]
    assert (fields["source_a_id"], fields["source_b_id"], fields["relationship"]) == (1, 2, "UNKNOWN")
    assert fields["disclosure"] is None and fields["supersedes_assessment_id"] is None


def test_superseding_requires_explicit_confirmation():
    at = app("history")
    at.selectbox[2].set_value("UNKNOWN")
    at.text_area[0].set_value("Synthetic: withdrawn")
    at.text_input[2].set_value("synthetic reviewer")
    at.button[0].click().run()

    assert "Confirm that the new assessment supersedes" in at.error[0].value
    assert at.session_state["saved"] == []

    at.checkbox[0].check()
    at.button[0].click().run()
    (fields,) = at.session_state["saved"]
    assert fields["supersedes_assessment_id"] == 2
