
import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

SPP = "097900BIBZ0000124550"   # public GLEIF LEI of SPP Infrastructure, a. s.

SETUP = """
import sys
from unittest.mock import MagicMock
sys.path[:0] = [{root!r}, {root!r} + "/app"]
import streamlit as st
import claim_subject_ui as ui

MODE = {mode!r}
SAVED = st.session_state.setdefault("saved", [])

def mapping(i, subject, status="MAPPED", lei={spp!r}, supersedes=None, candidates=None):
    return {{"mapping_id": i, "subject_text": subject, "status": status, "lei": lei,
             "name_type": "TRADING_NAME" if lei else None, "candidate_leis": candidates,
             "basis": "DOCUMENTED", "evidence_source_id": None,
             "evidence_reference": "GLEIF record", "evidence_location": "LegalName",
             "rationale": "Synthetic rationale", "limitations": "Synthetic limitation",
             "recorded_by": "synthetic reviewer", "recorded_at_utc": "2026-02-01",
             "supersedes_mapping_id": supersedes}}

DATA = {{
    "absent": (None, ["SPP Infrastructure"]),
    "no_claims": ([], []),
    "mixed": ([mapping(1, "SPP Infrastructure"),
               mapping(2, "Synthetic Group", "AMBIGUOUS", None,
                       candidates='["SYNTHA00000000000047", "SYNTHB00000000000016"]')],
              ["SPP Infrastructure", "Synthetic Group", "Synthetic Holdings"]),
    "empty": ([], ["SPP Infrastructure"]),
}}[MODE]
ui._load = lambda connect_db, case_id: (DATA[0], DATA[1], ({spp!r}, "315700I4J1M1NKGWWY89"),
                                        [{{"source_id": 1, "document_title": "Synthetic report"}}])

def save(connect_db, case_id, fields):
    SAVED.append(fields)
    return {{"mapping_id": 9, "created": True}}

ui._save = save
ui.render_observed_names = lambda leis, connect_db: st.session_state.setdefault('names_for', sorted(leis))
ui.render_claim_subjects(1, lambda: MagicMock())
"""


def app(mode):
    from pathlib import Path

    root = str(Path(__file__).resolve().parents[1])
    return AppTest.from_string(SETUP.format(root=root, mode=mode, spp=SPP),
                               default_timeout=30).run()


def test_absent_schema_and_empty_case_are_explained():
    absent = app("absent")
    assert not absent.exception
    assert "does not record claim-subject identities" in absent.info[0].value

    empty = app("no_claims")
    assert not empty.exception
    assert "has no claims" in empty.info[0].value


def test_subject_table_distinguishes_identity_states():
    at = app("mixed")

    assert not at.exception
    table = at.dataframe[0].value
    assert table["Identity"].tolist() == ["MAPPED", "AMBIGUOUS", "UNMAPPED"]
    assert table["LEI"].tolist()[0] == SPP
    assert "SYNTHA" in table["LEI"].tolist()[1]
    assert "more than one legal entity" in table["Notes"].tolist()[1]
    assert any("says nothing about ownership" in c.value for c in at.caption)


def test_confirmed_mapping_saved_with_its_fields():
    at = app("empty")
    at.text_input[0].set_value(SPP)
    at.selectbox[2].set_value("TRADING_NAME")
    at.text_input[2].set_value("GLEIF LEI record")
    at.text_input[3].set_value("Entity/LegalName")
    at.text_area[0].set_value("Synthetic: short form of the legal name")
    at.text_area[1].set_value("Synthetic limitation")
    at.text_input[4].set_value("synthetic reviewer")
    at.button[0].click().run()

    assert not at.exception
    (fields,) = at.session_state["saved"]
    assert (fields["subject_text"], fields["status"], fields["lei"]) == (
        "SPP Infrastructure", "MAPPED", SPP)
    assert fields["supersedes_mapping_id"] is None


def test_reassignment_requires_confirmation():
    at = app("mixed")
    at.text_area[0].set_value("Synthetic: reassessed")
    at.text_input[4].set_value("synthetic reviewer")
    at.button[0].click().run()

    assert "Confirm that the new mapping supersedes" in at.error[0].value
    assert at.session_state["saved"] == []

    at.checkbox[0].check()
    at.button[0].click().run()
    (fields,) = at.session_state["saved"]
    assert fields["supersedes_mapping_id"] == 1
