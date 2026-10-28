
"""Interface refinement: clean opening state, readable labels, duplicate-free histories."""

import datetime
import sys
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import display  # noqa: E402
from workflow_ui import audit_rows, review_rows, split_reviews  # noqa: E402

T = datetime.datetime(2026, 1, 1, 9, 0)


# Labels

def test_labels_are_readable_and_keep_unknown_codes_recognisable():
    assert display.relationship_type("IS_DIRECTLY_CONSOLIDATED_BY") == "Directly consolidated by"
    assert display.relationship_type("IS_ULTIMATELY_CONSOLIDATED_BY") == "Ultimately consolidated by"
    assert display.period_type("ACCOUNTING_PERIOD") == "Accounting period"
    assert display.relationship_type("IS_FEEDER_TO") == "Is feeder to"
    assert display.relationship_type(None) == "Not stated"


def test_gleif_classes_are_attributed_to_gleif_not_crie():
    labels = [display.gleif_validation(c) for c in display.GLEIF_VALIDATION]
    assert "Fully corroborated by GLEIF" in labels
    assert all("CRIE" not in label for label in labels)
    assert all(label.endswith("by GLEIF") or "corroborated" not in label.lower()
               for label in labels)


def test_timestamps_are_utc_labelled_and_missing_ends_are_not_stated():
    assert display.utc(datetime.datetime(2025, 1, 1)) == "2025-01-01 00:00 UTC"
    assert display.utc("2026-10-26T03:45:48.460503") == "2026-10-26 03:45:48 UTC"
    assert display.utc(None) == "Not stated"
    periods = pd.DataFrame([{"period_type": "RELATIONSHIP_PERIOD", "period_start_utc": T,
                             "period_end_utc": None, "period_source": "GLEIF_OBSERVATION"}])
    row = display.period_table(periods).iloc[0].to_dict()
    assert row == {"Period type": "Relationship period", "Start (UTC)": "2026-01-01 09:00 UTC",
                   "End (UTC)": "Not stated", "Recorded in": "GLEIF observation"}


# Assessment histories

def review(i, source, new, previous, reason, duplicate=False):
    return {"history_id": i, "source_id": source, "document_title": "Synthetic filing",
            "source_url": None, "event_type": "REVIEW", "previous_assessment": previous,
            "new_assessment": new, "review_reason": reason, "recorded_at_utc": T,
            "accidental_duplicate": duplicate}


def test_only_annotated_duplicates_leave_the_investigator_view():
    reviews = [review(1, 1, "SUPPORTS", "NOT_REVIEWED", "First reading"),
               review(2, 1, "SUPPORTS", "NOT_REVIEWED", "First reading", duplicate=True),
               # Same classification, new reasoning: a genuine revision.
               review(3, 1, "SUPPORTS", "SUPPORTS", "Re-read against the index")]
    entries, duplicates = split_reviews(reviews)

    assert [r["history_id"] for r in entries] == [1, 3]
    assert [r["Status"] for r in review_rows(entries)] == ["Earlier", "Current"]
    audit = audit_rows(duplicates)
    assert [(a["History ID"], a["Annotation"], a["Reasoning"]) for a in audit] == [
        (2, "ACCIDENTAL_DUPLICATE", "First reading")]
    assert audit[0]["Recorded"] == "2026-01-01 09:00 UTC"


HISTORY = """
import sys, datetime
sys.path[:0] = [{root!r}, {root!r} + "/app"]
import workflow_ui as ui
T = datetime.datetime(2026, 1, 1)
def row(i, duplicate, reason):
    return {{"history_id": i, "source_id": 1, "document_title": "Synthetic", "source_url": None,
             "event_type": "REVIEW", "previous_assessment": "NOT_REVIEWED",
             "new_assessment": "SUPPORTS", "review_reason": reason, "recorded_at_utc": T,
             "accidental_duplicate": duplicate}}
ui._load_history = lambda connect_db, case_id: (
    [row(1, False, "Reading"), row(2, True, "Reading"), row(3, False, "Second reading")], [])
ui.render_assessment_history(1, lambda: None)
"""


def test_case_history_keeps_duplicates_in_a_collapsed_audit_record():
    at = AppTest.from_string(HISTORY.format(root=str(ROOT)), default_timeout=30).run()

    assert not at.exception
    assert at.dataframe[0].value["Reasoning"].tolist() == ["Reading", "Second reading"]
    audit = [e for e in at.expander if e.label.startswith("Audit record")]
    assert [e.label for e in audit] == ["Audit record: 1 annotated accidental duplicate review(s)"]
    assert audit[0].proto.expanded is False
    assert at.dataframe[1].value["History ID"].tolist() == [2]
    assert not at.checkbox                                  # nothing to click to hide clutter


EDITOR = """
import sys, datetime
sys.path[:0] = [{root!r}, {root!r} + "/app"]
import assessment_editor
T = datetime.datetime(2026, 1, 1)
HISTORY = [
    {{"history_id": 3, "event_type": "REVIEW", "previous_assessment": "SUPPORTS",
      "new_assessment": "SUPPORTS", "review_reason": "Second reading", "recorded_at_utc": T,
      "entry_status": "RETAINED"}},
    {{"history_id": 2, "event_type": "REVIEW", "previous_assessment": "NOT_REVIEWED",
      "new_assessment": "SUPPORTS", "review_reason": "Reading", "recorded_at_utc": T,
      "entry_status": "ACCIDENTAL DUPLICATE"}},
    {{"history_id": 1, "event_type": "REVIEW", "previous_assessment": "NOT_REVIEWED",
      "new_assessment": "SUPPORTS", "review_reason": "Reading", "recorded_at_utc": T,
      "entry_status": "RETAINED"}},
]
class Cursor:
    def execute(self, sql, params=None): pass
    def fetchall(self): return HISTORY
    def close(self): pass
class Connection:
    def cursor(self, **kwargs): return Cursor()
    def close(self): pass
assessment_editor.render_assessment_editor(
    1, [{{"source_id": 7, "document_title": "Synthetic filing", "evidence_assessment": "SUPPORTS"}}],
    Connection)
"""


def test_document_history_has_no_duplicate_toggle_and_keeps_the_audit_record():
    at = AppTest.from_string(EDITOR.format(root=str(ROOT)), default_timeout=30).run()

    assert not at.exception
    assert not [c for c in at.checkbox if "duplicate" in c.label.lower()]
    main = at.dataframe[0].value
    assert main["History ID"].tolist() == [3, 1]
    assert "Entry status" not in main.columns
    assert [e.label for e in at.expander] == [
        "Audit record: 1 annotated accidental duplicate review(s)"]
    assert at.dataframe[1].value["History ID"].tolist() == [2]


# Review queue: nothing opens until chosen; existing investigations come first;
# relationships are chosen by stable key and opened through an explicit action.

QUEUE = """
import sys
import pandas as pd
sys.path[:0] = [{root!r}, {root!r} + "/app"]
import streamlit as st
import review_queue
review_queue.render_gleif_history = lambda case: None
def row(name, lei, validation):
    return {{"child_name": name, "parent_name": "Synthetic Parent", "child_lei": lei,
             "parent_lei": "B" * 20, "relationship_type": "IS_DIRECTLY_CONSOLIDATED_BY",
             "validation_sources": validation,
             "validation_documents": "OTHER_OFFICIAL_DOCUMENTS", "last_update_at_utc": None,
             "snapshot_at_utc": pd.Timestamp("2026-01-01"), "source_file": "synthetic.xml",
             "validation_reference": None}}
rows = [row("Synthetic Alpha", "A" * 20, "ENTITY_SUPPLIED_ONLY"),
        row("Synthetic Beta", "C" * 20, "ENTITY_SUPPLIED_ONLY"),
        row("Synthetic Gamma", "D" * 20, "FULLY_CORROBORATED")]
# Case 9 is anchored on an earlier observation than the row now loaded.
cases = pd.DataFrame([{{"case_id": 9, "child_lei": "C" * 20, "parent_lei": "B" * 20,
                        "relationship_type": "IS_DIRECTLY_CONSOLIDATED_BY",
                        "snapshot_at_utc": pd.Timestamp("2025-06-01"),
                        "review_status": "IN_PROGRESS"}}])
network = {network!r}
review_queue.render_review_queue(pd.DataFrame(rows), pd.DataFrame(), cases,
                                 set(network) if network else None, "Synthetic Parent")
"""

KEY = "|" + "B" * 20 + "|IS_DIRECTLY_CONSOLIDATED_BY"


def queue_app(network=None):
    return AppTest.from_string(QUEUE.format(root=str(ROOT), network=network),
                               default_timeout=30).run()


def test_queue_lists_existing_investigations_first_and_opens_nothing_by_itself():
    at = queue_app()

    assert not at.exception
    choice = at.selectbox(key="crie_selected_relationship")
    assert choice.value is None
    assert choice.options[0].startswith("Case #9 · Synthetic Beta")  # existing case first
    queue = at.dataframe[0].value
    assert queue["Investigation"].tolist() == ["Case #9", "—"]       # found despite a later row
    assert queue["Relationship"].tolist() == ["Directly consolidated by"] * 2
    assert queue["GLEIF validation class"].tolist() == ["Entity-supplied only"] * 2

    choice.select_index(0).run()
    assert at.session_state["crie_relationship"] == "C" * 20 + KEY   # stable key, not position
    at.button(key="crie_queue_investigate").click().run()
    assert at.session_state["crie_open_case"] == 9                   # opened, never duplicated
    assert at.session_state["crie_pending_relationship"] is None


def test_selected_network_lists_every_relationship_and_offers_a_new_investigation():
    at = queue_app(network=["A" * 20, "B" * 20, "C" * 20, "D" * 20])

    assert not at.exception
    assert at.radio(key="crie_queue_scope").value == "Selected network"
    labels = [m.label for m in at.metric]
    assert labels == ["Relationships in the selected network"]
    assert at.metric[0].value == "3"                                 # includes the corroborated one

    choice = at.selectbox(key="crie_selected_relationship")
    choice.set_value("D" * 20 + KEY).run()
    assert "Fully corroborated by GLEIF" in " ".join(m.value for m in at.markdown)
    at.button(key="crie_queue_investigate").click().run()
    assert at.session_state["crie_pending_relationship"] == "D" * 20 + KEY
    assert at.session_state["crie_open_case"] is None

    at.radio(key="crie_queue_scope").set_value("Whole dataset (flagged)").run()
    assert at.metric[0].value == "2"


# Dashboard: a clean opening state, labelled scope, and a demonstration banner.

DASHBOARD = """
import sys
sys.path[:0] = [{root!r}, {root!r} + "/app"]
exec(compile(open({dashboard!r}).read(), {dashboard!r}, "exec"))
"""

ROW = {"child_name": "Synthetic Harbour plc", "parent_name": "Synthetic Parent AG",
       "child_lei": "A" * 20, "parent_lei": "B" * 20, "child_jurisdiction": "GB",
       "parent_jurisdiction": "CH", "relationship_type": "IS_DIRECTLY_CONSOLIDATED_BY",
       "validation_sources": "ENTITY_SUPPLIED_ONLY",
       "validation_documents": "OTHER_OFFICIAL_DOCUMENTS", "validation_reference": None,
       "last_update_at_utc": None, "snapshot_at_utc": pd.Timestamp("2026-01-01"),
       "source_file": "synthetic.xml"}


class NoRows:
    """A stand-in connection: no database is reached."""

    def cursor(self, **kwargs):
        return self

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return []

    def fetchone(self):
        return (0,)                      # e.g. no ownership schema in this database

    def close(self):
        pass

    def rollback(self):
        pass


@pytest.fixture
def dashboard(monkeypatch):
    # Patched for this test only; AppTest runs the script in this process.
    import dotenv
    import mysql.connector
    import src.relationship_context

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: True)
    monkeypatch.setattr(mysql.connector, "connect", lambda **kwargs: NoRows())
    monkeypatch.setattr(src.relationship_context, "load_relationship_context",
                        lambda connection: ([ROW], [], None))

    def open_dashboard(database):
        monkeypatch.setenv("MYSQL_DATABASE", database)
        return AppTest.from_string(DASHBOARD.format(root=str(ROOT),
                                                    dashboard=str(ROOT / "app/dashboard.py")),
                                   default_timeout=30).run()

    return open_dashboard


def test_dashboard_opens_without_a_company_or_investigation(dashboard):
    at = dashboard("corporate_intelligence")

    assert not at.exception
    search = at.selectbox(key="crie_selected_company")
    assert search.label == "Search for a company" and search.value is None
    assert not at.sidebar.selectbox                                  # search is on the page
    # Only the Research leads tab's whole-dataset count; no network yet.
    assert [m.label for m in at.metric] == ["Flagged relationships (whole dataset)"]
    assert any("Search for a company above" in i.value for i in at.info)
    assert not at.warning or not any("Demonstration database" in w.value for w in at.warning)
    assert any("evidence-grounded corporate intelligence platform" in m.value
               for m in at.markdown)
    assert [t.label for t in at.tabs] == ["Evidence", "Timeline", "Research leads"]


def test_selected_company_is_labelled_and_keeps_stored_values_in_technical_details(dashboard):
    at = dashboard("corporate_intelligence")
    at.selectbox(key="crie_selected_company").set_value("A" * 20).run()

    assert not at.exception
    assert at.session_state["crie_company"] == "A" * 20              # carries across pages
    labels = [m.label for m in at.metric]
    assert labels[:3] == ["Entities in this network", "Reported relationships in this network",
                          "Outside GLEIF's fully corroborated class"]
    evidence = at.dataframe[0].value
    assert evidence["Relationship"].tolist() == ["Directly consolidated by"]
    assert evidence["GLEIF validation class"].tolist() == ["Entity-supplied only"]
    stored = at.dataframe[1].value
    assert stored["relationship_type"].tolist() == ["IS_DIRECTLY_CONSOLIDATED_BY"]
    assert stored["validation_sources"].tolist() == ["ENTITY_SUPPLIED_ONLY"]
    assert any("not evidence of direct share ownership" in c.value for c in at.caption)
    # A relationship of this network can be chosen and investigated, right below it.
    picker = at.selectbox(key="crie_explore_relationship")
    assert picker.options == ["Synthetic Harbour plc → Synthetic Parent AG (Directly consolidated by)"]
    picker.select_index(0).run()
    assert at.session_state["crie_relationship"] == "A" * 20 + KEY
    assert at.button(key="crie_explore_relationship_go").label == "Investigate this relationship"


def test_demonstration_database_is_marked(dashboard):
    at = dashboard("corporate_intelligence_e2e")

    assert not at.exception
    assert any("Demonstration database" in w.value and "synthetic data" in w.value
               for w in at.warning)
