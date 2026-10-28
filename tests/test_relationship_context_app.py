
"""
How the review queue and case management present evidence rows created by
GLEIF import batches (23B.2B-2). No database.
"""

from pathlib import Path

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

from src.relationship_context import LEGACY_PERIODS_SQL, relationships_sql  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

QUEUE = """
import sys
import pandas as pd
sys.path[:0] = [{root!r}, {root!r} + "/app"]
import review_queue

review_queue.render_case_management = lambda case: None
review_queue.render_gleif_history = lambda case: None
row = {{
    "child_name": "Fictional Child", "parent_name": "Fictional Parent",
    "child_lei": "A" * 20, "parent_lei": "B" * 20,
    "relationship_type": "IS_DIRECTLY_CONSOLIDATED_BY",
    "validation_sources": "ENTITY_SUPPLIED_ONLY",
    "validation_documents": "OTHER_OFFICIAL_DOCUMENTS",
    "last_update_at_utc": None, "snapshot_at_utc": pd.Timestamp("2026-11-01"),
    "source_file": "fictional.xml", "validation_reference": None,
}}
row.update({extra!r})
periods = pd.DataFrame({periods!r})
if not periods.empty:
    periods["snapshot_at_utc"] = pd.Timestamp("2026-11-01")
review_queue.render_review_queue(pd.DataFrame([row]), periods)
"""

BATCH = {"anchor_origin": "GLEIF_BATCH", "anchor_batch_id": 7,
         "anchor_selection_mode": "FIRST_N", "anchor_publication_scope": "FULL",
         "anchor_observations": 1}


def queue(extra, periods=()):
    periods = [{"child_lei": "A" * 20, "parent_lei": "B" * 20,
                "relationship_type": "IS_DIRECTLY_CONSOLIDATED_BY", **p} for p in periods]
    at = AppTest.from_string(QUEUE.format(root=str(ROOT), extra=extra, periods=periods),
                             default_timeout=30).run()
    # Nothing is selected until the investigator chooses a relationship.
    assert at.selectbox(key="crie_selected_relationship").value is None
    return at.selectbox(key="crie_selected_relationship").select_index(0).run()


def table(at, column):
    """The first displayed table with this column."""

    return next(df.value for df in at.dataframe if column in df.value.columns)


def text(at):
    return " ".join(e.value for e in (*at.warning, *at.info))


def test_batch_anchor_is_explained_and_its_observation_periods_shown():
    at = queue(BATCH, periods=[
        {"period_type": "ACCOUNTING_PERIOD", "period_start_utc": "2024-01-01",
         "period_end_utc": "2024-12-31", "period_source": "GLEIF_OBSERVATION"},
        # A legacy period row for the same key is not mixed in.
        {"period_type": "DOCUMENT_FILING_PERIOD", "period_start_utc": "2020-01-01",
         "period_end_utc": None, "period_source": "LEGACY_PERIOD_TABLE"},
    ])

    assert not at.exception
    assert "GLEIF import batch #7" in text(at)
    assert "a sample of a GLEIF file, not the complete dataset" in text(at)
    assert "not the date of any corporate change" in text(at)
    stored, readable = table(at, "period_type"), table(at, "Period type")
    assert stored["period_type"].tolist() == ["ACCOUNTING_PERIOD"]         # stored values kept
    assert stored["period_source"].tolist() == ["GLEIF_OBSERVATION"]
    assert readable["Period type"].tolist() == ["Accounting period"]
    assert readable["Recorded in"].tolist() == ["GLEIF observation"]
    assert "No period information available." not in text(at)


def test_batch_anchor_without_stated_periods_is_not_called_missing():
    at = queue({**BATCH, "anchor_selection_mode": "ALL_RECORDS"})

    assert not at.exception
    assert "a complete import of a full published GLEIF file" in text(at)
    assert "states no periods" in text(at)
    assert "No period information available." not in text(at)


def test_conflicting_versions_in_the_anchoring_file_are_flagged():
    at = queue({**BATCH, "anchor_observations": 2})
    assert "reported this relationship in 2 different ways" in text(at)


@pytest.mark.parametrize("extra", [{}, {"anchor_origin": "LEGACY_IMPORT", "anchor_batch_id": None}])
def test_legacy_rows_are_presented_as_before(extra):
    at = queue(extra, periods=[{"period_type": "ACCOUNTING_PERIOD",
                                "period_start_utc": "2023-01-01", "period_end_utc": "2023-12-31"}])

    assert not at.exception
    assert "GLEIF import batch" not in text(at)
    assert table(at, "period_type")["period_type"].tolist() == ["ACCOUNTING_PERIOD"]
    assert table(at, "Period type")["Period type"].tolist() == ["Accounting period"]


CASE = """
import sys
import pandas as pd
sys.path[:0] = [{root!r}, {root!r} + "/app"]
import streamlit as st
import case_management as cm

SAVED = st.session_state.setdefault("saved", [])
cm.get_case = lambda case: None
cm.get_relationship_cases = lambda case: []
cm.save_case = lambda case, status, assessment, note, url: SAVED.append(status) or 1
case = pd.Series({{
    "child_lei": "A" * 20, "parent_lei": "B" * 20,
    "relationship_type": "IS_DIRECTLY_CONSOLIDATED_BY",
    "snapshot_at_utc": pd.Timestamp("2026-11-01"), **{extra!r}}})
cm.render_case_management(case)
"""


def case_app(extra):
    return AppTest.from_string(CASE.format(root=str(ROOT), extra=extra), default_timeout=30).run()


def test_new_case_on_a_batch_anchor_needs_acknowledgement():
    at = case_app({"anchor_origin": "GLEIF_BATCH", "anchor_batch_id": 7})
    assert "GLEIF import batch #7" in at.checkbox[0].label

    at.button[0].click().run()
    assert "Confirm that you understand the limits" in at.error[0].value
    assert at.session_state["saved"] == []

    at.checkbox[0].check()
    at.button[0].click().run()
    assert at.session_state["saved"] == ["OPEN"]


def test_legacy_anchor_case_needs_no_acknowledgement():
    at = case_app({"anchor_origin": "LEGACY_IMPORT"})
    assert len(at.checkbox) == 0
    at.button[0].click().run()
    assert at.session_state["saved"] == ["OPEN"]


def test_legacy_queries_have_no_observation_dependency():
    for sql in (relationships_sql(False), LEGACY_PERIODS_SQL):
        assert "gleif" not in sql and "first_batch_id" not in sql and "observation" not in sql
    installed = relationships_sql(True)
    assert "b2.status = 'COMPLETED'" in installed and "b.status = 'COMPLETED'" in installed


def test_quarantine_in_the_anchoring_batch_is_not_a_trusted_dataset():
    at = queue({**BATCH, "anchor_selection_mode": "ALL_RECORDS", "anchor_quarantined_records": 2})
    assert "some records were quarantined (not a fully trusted dataset)" in text(at)
    assert "a complete import of a full published GLEIF file" not in text(at)
