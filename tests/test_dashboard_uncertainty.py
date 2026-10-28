
"""
Dashboard uncertainty (Milestone 23A, A2/A3): missing periods are
detected before text conversion, agreement is not shown as
corroboration, and empty inputs do not crash.
"""

import datetime
import sys
from pathlib import Path

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "app")]

from disclosure_panel import analyze_case_claims  # noqa: E402


def record(claim_id, source_id, value, year, period_end, metric="interest_pct",
           publisher="Fictional plc", url=None):
    return {
        "claim_id": claim_id, "source_id": source_id, "entity": "Fictional Holdings",
        "metric": metric, "report_year": year, "period_end": period_end,
        "value": value, "source_location": "Page 3",
        "document_title": f"Fictional report {source_id}",
        "source_url": url or f"https://example.org/{source_id}.pdf",
        "publisher": publisher, "publication_date": None,
        "evidence_assessment": "SUPPORTS",
    }


D24, D25 = datetime.date(2024, 12, 31), datetime.date(2025, 12, 31)


def test_missing_periods_are_excluded_not_grouped_as_consistent():
    analysis = analyze_case_claims([
        record(1, 1, "49", 2025, None),
        record(2, 2, "49", 2025, None, publisher="Other publisher"),
        record(3, 1, "49", 2025, D25),
    ])

    assert sorted(analysis["incomplete"]["claim_id"]) == [1, 2]
    statuses = analysis["reconciliation"]["reconciliation_status"].tolist()
    # Only the dated claim is analysed; the undated pair is not "consistent".
    assert statuses == ["SINGLE_CLAIM"]
    assert "None" not in analysis["reconciliation"]["period_end"].tolist()


def test_blank_values_are_incomplete_too():
    analysis = analyze_case_claims([record(1, 1, "  ", 2025, D25), record(2, 1, "49", 2025, D25)])

    assert analysis["incomplete"]["claim_id"].tolist() == [1]


def test_other_periods_unaffected_and_compared():
    analysis = analyze_case_claims([
        record(1, 1, "49", 2024, D24), record(2, 1, "49", 2025, D25),
        record(3, 1, "51", 2025, None, metric="other_pct"),
    ])

    assert analysis["years"] == ("2024", "2025")
    (row,) = analysis["comparison"].to_dict("records")
    assert row["comparison_status"] == "UNCHANGED"


def test_mismatched_periods_are_reported():
    analysis = analyze_case_claims([
        record(1, 1, "49", 2024, datetime.date(2024, 9, 30)),
        record(2, 1, "49", 2025, D25),
    ])

    (row,) = analysis["comparison"].to_dict("records")
    assert row["comparison_status"] == "PERIOD_MISMATCH"


def test_agreement_classified_with_source_metadata():
    same_document = analyze_case_claims([
        record(1, 1, "49", 2025, D25, url="https://example.org/r.pdf"),
        record(2, 2, "49", 2025, D25, url="https://example.org/r.pdf#page=3"),
    ])
    same_publisher = analyze_case_claims([record(1, 1, "49", 2025, D25), record(2, 2, "49", 2025, D25)])
    unknown = analyze_case_claims([
        record(1, 1, "49", 2025, D25), record(2, 2, "49", 2025, D25, publisher="Another plc"),
    ])

    status = lambda a: a["reconciliation"]["reconciliation_status"].iloc[0]  # noqa: E731
    assert status(same_document) == "DUPLICATE_DOCUMENT_CLAIMS"
    assert status(same_publisher) == "CONSISTENT_RELATED_SOURCES"
    assert status(unknown) == "INDEPENDENCE_UNVERIFIED"
    for analysis in (same_document, same_publisher, unknown):
        assert not analysis["reconciliation"]["corroborated"].any()


@pytest.mark.parametrize("records", [[], [record(1, 1, "49", 2025, None)]])
def test_empty_or_fully_incomplete_inputs_do_not_crash(records):
    analysis = analyze_case_claims(records)

    assert analysis["reconciliation"] is None and analysis["error"] is None


PANEL = """
import sys, datetime
from unittest.mock import MagicMock
sys.path[:0] = [{root!r}, {root!r} + "/app"]
import disclosure_panel as panel

panel.render_claim_correction_ui = lambda case_id, connect_db: None
panel.render_claim_subjects = lambda case_id, connect_db: None
RECORDS = {records!r}
for r in RECORDS:
    if r["period_end"]:
        r["period_end"] = datetime.date.fromisoformat(r["period_end"])

def connect():
    connection = MagicMock()
    connection.cursor.return_value.fetchall.return_value = RECORDS
    return connection

panel.render_disclosure_panel(1, connect)
"""


def panel(records):
    for r in records:
        r["period_end"] = r["period_end"].isoformat() if r["period_end"] else None
    return AppTest.from_string(
        PANEL.format(root=str(ROOT), records=records), default_timeout=30
    ).run()


def test_panel_explains_single_claims_and_agreement():
    at = panel([
        record(1, 1, "49", 2024, D24), record(2, 1, "49", 2025, D25),
        record(3, 1, "51", 2025, D25, metric="other_pct"),
        record(4, 2, "51", 2025, D25, metric="other_pct", publisher="Another plc"),
    ])

    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Single claims (not corroborated):** 2" in text
    assert "Agreement without established independence:** 1" in text
    assert "Independently corroborated:** 0" in text
    assert "No competing claims were detected" not in str(at)
    meanings = at.dataframe[1].value["Meaning"].tolist()
    assert any("Not corroboration" in m for m in meanings)


def test_panel_flags_missing_periods():
    at = panel([record(1, 1, "49", 2025, None), record(2, 2, "49", 2025, None),
                record(3, 1, "49", 2025, D25)])

    assert not at.exception
    assert "2 claim(s) lack a required field" in at.warning[0].value
    assert "#1, #2" in at.warning[0].value
    assert at.dataframe[0].value["Period End"].tolist().count("Not recorded") == 2


QUEUE = """
import sys
import pandas as pd
sys.path[:0] = [{root!r}, {root!r} + "/app"]
import review_queue

review_queue.render_case_management = lambda case: None
review_queue.render_gleif_history = lambda case: None
relationships = pd.DataFrame([{{
    "child_name": "Fictional Child", "parent_name": "Fictional Parent",
    "child_lei": "A" * 20, "parent_lei": "B" * 20,
    "relationship_type": "IS_DIRECTLY_CONSOLIDATED_BY",
    "validation_sources": "ENTITY_SUPPLIED_ONLY",
    "validation_documents": "OTHER_OFFICIAL_DOCUMENTS",
    "last_update_at_utc": None, "snapshot_at_utc": pd.Timestamp("2026-01-01"),
    "source_file": "fictional.xml", "validation_reference": None,
}}])
review_queue.render_review_queue(relationships, pd.DataFrame())
"""


def test_review_queue_survives_empty_periods_and_explains_gleif_class():
    at = AppTest.from_string(QUEUE.format(root=str(ROOT)), default_timeout=30).run()
    at.selectbox(key="crie_selected_relationship").select_index(0).run()

    assert not at.exception
    assert any("No period information available." in i.value for i in at.info)
    assert any("not a CRIE corroboration finding" in c.value for c in at.caption)
