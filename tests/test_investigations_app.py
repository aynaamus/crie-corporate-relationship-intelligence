
"""The Investigations page: cases by permanent ID, on their own anchor, one section at a time."""

import sys
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import investigations  # noqa: E402

A, B, C = "A" * 20, "B" * 20, "C" * 20
DIRECT = "IS_DIRECTLY_CONSOLIDATED_BY"


def test_continuity_notes_keep_the_case_on_its_anchor():
    case = {"child_lei": A, "parent_lei": B, "relationship_type": DIRECT,
            "snapshot_at_utc": pd.Timestamp("2026-06-01 09:00"),
            "current_relationship_status": "INACTIVE"}
    later = pd.DataFrame([{"child_lei": A, "parent_lei": B, "relationship_type": DIRECT,
                           "snapshot_at_utc": pd.Timestamp("2026-10-01 09:00")}])
    notes = investigations.continuity_notes(case, later)
    assert "no longer lists this relationship as active" in notes[0]
    assert "not evidence of when, or whether, the relationship ended" in notes[0]
    assert "data valid as of 2026-10-01 09:00 UTC" in notes[1]
    assert "stays anchored on its original evidence row" in notes[1]

    current = {**case, "current_relationship_status": "ACTIVE",
               "snapshot_at_utc": pd.Timestamp("2026-10-01 09:00")}
    assert investigations.continuity_notes(current, later) == []


PAGE = """
import sys
import pandas as pd
sys.path[:0] = [{root!r}, {root!r} + "/app"]
import streamlit as st
import investigations as inv
CALLS = st.session_state.setdefault("calls", [])
inv.load_anchor = lambda connect_db, case_id: {{
    "case_id": case_id, "child_lei": "{a}", "parent_lei": "{b}",
    "relationship_type": "{direct}", "snapshot_at_utc": pd.Timestamp("2026-06-01 09:00"),
    "child_name": "Synthetic Harbour", "parent_name": "Synthetic Parent"}}
inv.render_case_management = lambda row: CALLS.append(("case", row.get("case_id"),
                                                       str(row["snapshot_at_utc"])))
inv.render_relationship_detail = lambda row, periods, investigation, action=None: CALLS.append(
    ("relationship", investigation))
inv.render_source_registry = lambda case_id, connect_db: CALLS.append(("sources", case_id))
inv.render_disclosure_panel = lambda case_id, connect_db: CALLS.append(("claims", case_id))
inv.render_report_history = lambda case_id, connect_db: CALLS.append(("reports", case_id))
inv.case_management.get_relationship_cases = lambda row: []
relationships = pd.DataFrame([{{
    "child_lei": lei, "parent_lei": "{b}", "relationship_type": "{direct}",
    "child_name": name, "parent_name": "Synthetic Parent",
    "validation_sources": "FULLY_CORROBORATED", "snapshot_at_utc": pd.Timestamp("2026-10-01 09:00")}}
    for lei, name in (("{a}", "Synthetic Harbour"), ("{c}", "Synthetic Other"))])
cases = pd.DataFrame([{{
    "case_id": 7, "child_lei": "{a}", "parent_lei": "{b}", "relationship_type": "{direct}",
    "snapshot_at_utc": pd.Timestamp("2026-06-01 09:00"), "review_status": "IN_PROGRESS",
    "assessment": "NOT_ASSESSED", "child_name": "Synthetic Harbour",
    "parent_name": "Synthetic Parent", "current_relationship_status": "INACTIVE"}}])
for key, value in {state!r}.items():
    st.session_state.setdefault(key, value)
inv.render_investigations(relationships, pd.DataFrame(), cases, None, None, lambda: None)
"""


def page(state):
    return AppTest.from_string(PAGE.format(root=str(ROOT), a=A, b=B, c=C, direct=DIRECT,
                                           state=state), default_timeout=30).run()


def test_cases_are_listed_by_id_and_nothing_opens_by_itself():
    at = page({})

    assert not at.exception
    assert at.dataframe[0].value["Case"].tolist() == ["#7"]
    assert at.selectbox(key="crie_case_picker").value is None
    assert at.session_state["calls"] == []


def test_an_inactive_case_opens_on_its_own_anchor_one_section_at_a_time():
    at = page({"crie_open_case": 7})

    assert not at.exception
    assert any("Case #7 · Synthetic Harbour → Synthetic Parent" in m.value for m in at.markdown)
    assert any("no longer lists this relationship as active" in i.value for i in at.info)
    assert any("later GLEIF observation" in i.value for i in at.info)
    # The case section is shown on the anchor row (case 7, its June observation).
    assert at.session_state["calls"] == [("case", 7, "2026-06-01 09:00:00")]

    expected = {"Relationship and GLEIF history": ("relationship", "Case #7"),
                "Sources and assessments": ("sources", 7),
                "Claims, identities and corrections": ("claims", 7),
                "Reports": ("reports", 7)}
    for section, call in expected.items():
        at.radio(key="crie_section").set_value(section).run()
        assert at.session_state["calls"][-1] == call
        assert at.session_state["crie_open_section"] == section
    assert len(at.session_state["calls"]) == 5                       # one section per run


def test_a_pending_relationship_with_a_case_opens_that_case_instead_of_a_new_one():
    at = page({"crie_pending_relationship": f"{A}|{B}|{DIRECT}"})

    assert not at.exception
    assert at.session_state["crie_open_case"] == 7
    assert at.session_state["crie_pending_relationship"] is None


def test_a_new_relationship_is_offered_for_a_first_investigation():
    at = page({"crie_pending_relationship": f"{C}|{B}|{DIRECT}"})

    assert not at.exception
    assert any("New investigation" in m.value for m in at.markdown)
    # The form is shown on the relationship's current row; nothing is saved yet.
    assert at.session_state["calls"] == [("relationship", "Not opened yet"),
                                         ("case", None, "2026-10-01 09:00:00")]


CASE_FORM = """
import sys
import pandas as pd
sys.path[:0] = [{root!r}, {root!r} + "/app"]
import case_management as cm
cm.get_case = lambda case: None
cm.get_relationship_cases = lambda case: [3]
cm.render_case_management(pd.Series({{"child_lei": "{a}", "parent_lei": "{b}",
    "relationship_type": "{direct}", "snapshot_at_utc": pd.Timestamp("2026-10-01")}}))
"""


def test_an_investigated_relationship_is_never_opened_twice():
    at = AppTest.from_string(CASE_FORM.format(root=str(ROOT), a=A, b=B, direct=DIRECT),
                             default_timeout=30).run()

    assert not at.exception
    assert "already investigated in Case #3" in at.info[0].value
    assert not at.button                                             # no form to save
