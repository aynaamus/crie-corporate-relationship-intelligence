
"""Stage 23B.3 workflow refinements: assessment history, navigation, claim corrections, glossary."""

import datetime
import sys
from pathlib import Path

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from claim_correction_ui import claim_status_lines  # noqa: E402
from glossary import GLOSSARY  # noqa: E402
from workflow_ui import relationship_rows, review_rows  # noqa: E402

T = datetime.datetime(2026, 1, 1, 9, 0)


def review(i, source, new, previous=None, duplicate=False):
    return {"history_id": i, "source_id": source, "document_title": f"Synthetic report {source}",
            "source_url": f"https://example.invalid/{source}.pdf", "event_type": "REVIEW",
            "previous_assessment": previous, "new_assessment": new,
            "review_reason": f"Synthetic reason {i}", "recorded_at_utc": T,
            "accidental_duplicate": duplicate}


def relationship(i, relationship, supersedes=None):
    return {"assessment_id": i, "case_id": 1, "source_a_id": 1, "source_b_id": 2,
            "entity_name": None, "metric": None, "report_year": None, "period_end": None,
            "relationship": relationship, "basis": "DOCUMENTED", "evidence_source_id": None,
            "evidence_reference": "Synthetic register", "evidence_location": "Entry 2",
            "rationale": "Synthetic", "limitations": None, "recorded_by": "synthetic reviewer",
            "recorded_at_utc": T, "supersedes_assessment_id": supersedes}


# 1. Assessment history

def test_review_history_marks_current_earlier_and_kept_duplicates():
    rows = review_rows([review(1, 1, "NOT_REVIEWED"), review(2, 1, "SUPPORTS", "NOT_REVIEWED"),
                        review(3, 1, "SUPPORTS", "SUPPORTS", duplicate=True),
                        review(4, 2, "INCONCLUSIVE")])
    assert [r["Status"] for r in rows] == ["Earlier", "Current",
                                           "Accidental duplicate (annotated, kept)", "Current"]
    assert rows[1]["Previous"] == "NOT_REVIEWED" and rows[0]["Previous"] == "—"
    assert rows[1]["Evidence"] == "https://example.invalid/1.pdf"


def test_relationship_assessments_keep_superseded_entries():
    rows = relationship_rows([relationship(1, "UNKNOWN"),
                              relationship(2, "SAME_CORPORATE_GROUP", supersedes=1)])
    assert [(r["Relationship"], r["Status"]) for r in rows] == [
        ("UNKNOWN", "Superseded"), ("SAME_CORPORATE_GROUP", "Current")]
    assert rows[0]["Evidence"] == "Synthetic register; Entry 2"


HISTORY = """
import sys, datetime
sys.path[:0] = [{root!r}, {root!r} + "/app"]
import workflow_ui as ui
T = datetime.datetime(2026, 1, 1)
MODE = {mode!r}
def load(connect_db, case_id):
    if MODE == "fail":
        raise RuntimeError("synthetic outage")
    reviews = [{{"history_id": 1, "source_id": 1, "document_title": "Synthetic", "source_url": None,
                 "event_type": "BASELINE", "previous_assessment": None, "new_assessment": "NOT_REVIEWED",
                 "review_reason": None, "recorded_at_utc": T, "accidental_duplicate": False}}]
    return (reviews if MODE == "full" else []), (None if MODE == "legacy" else [])
ui._load_history = load
ui.render_assessment_history(1, lambda: None)
"""


@pytest.mark.parametrize("mode, expected", [
    ("full", "No source-relationship assessments are recorded for this investigation."),
    ("legacy", "This database does not record source-relationship assessments."),
    ("empty", "No evidence reviews are recorded for this investigation."),
])
def test_assessment_history_view(mode, expected):
    at = AppTest.from_string(HISTORY.format(root=str(ROOT), mode=mode), default_timeout=30).run()
    assert not at.exception
    assert expected in " ".join(i.value for i in at.info)
    assert at.subheader[0].value == "Assessment history"
    if mode == "full":
        assert at.dataframe[0].value["Status"].tolist() == ["Current"]
        assert at.dataframe[0].value["Reasoning"].tolist() == ["Not recorded"]


def test_assessment_history_failure_is_shown():
    at = AppTest.from_string(HISTORY.format(root=str(ROOT), mode="fail"), default_timeout=30).run()
    assert not at.exception
    assert "Assessment history is unavailable: synthetic outage" in at.error[0].value


# 2. Relationship selection

QUEUE = """
import sys
import pandas as pd
sys.path[:0] = [{root!r}, {root!r} + "/app"]
import review_queue
review_queue.render_case_management = lambda case: None
review_queue.render_gleif_history = lambda case: None
rows = [{{"child_name": f"Child {{i}}", "parent_name": "Parent", "child_lei": str(i) * 20,
          "parent_lei": "B" * 20, "relationship_type": "IS_DIRECTLY_CONSOLIDATED_BY",
          "validation_sources": "ENTITY_SUPPLIED_ONLY", "validation_documents": "OTHER_OFFICIAL_DOCUMENTS",
          "last_update_at_utc": None, "snapshot_at_utc": pd.Timestamp("2026-01-01"),
          "source_file": "f.xml", "validation_reference": None}} for i in (1, 2)]
review_queue.render_review_queue(pd.DataFrame(rows), pd.DataFrame())
"""


def test_selected_relationship_persists_across_reruns():
    at = AppTest.from_string(QUEUE.format(root=str(ROOT)), default_timeout=30).run()
    at.selectbox(key="crie_selected_relationship").select_index(1).run()
    at.run()
    # Selected by its stable relationship key, not by row position.
    key = "2" * 20 + "|" + "B" * 20 + "|IS_DIRECTLY_CONSOLIDATED_BY"
    assert at.session_state["crie_selected_relationship"] == key
    assert at.session_state["crie_relationship"] == key
    assert "Child 2" in " ".join(m.value for m in at.markdown)


# 3. Claim-correction visibility

def lineage(*versions):
    return {"versions": [{"claim_id": i, "value_text": v, "source_location": loc}
                         for i, (v, loc) in enumerate(versions, 1)]}


def test_claim_status_explains_what_is_recorded_and_possible():
    only = claim_status_lines({"claim_id": 1, "value_text": "49", "source_location": "Page 3"},
                              lineage(("49", "Page 3")))
    assert "only recorded version" in only[1] and "Restoring is not available" in only[2]

    corrected = claim_status_lines({"claim_id": 3, "value_text": "52", "source_location": "Page 4"},
                                   lineage(("49", "Page 3"), ("51", "Page 4"), ("52", "Page 4")))
    assert 'claim #3 (the active version), value "52" at "Page 4"' in corrected[0]
    assert "3 recorded versions; the 2 earlier one(s) are superseded and kept unchanged" in corrected[1]
    assert corrected[2].endswith("or restore an earlier version.")


def test_correction_screen_shows_status_and_how_it_works():
    from test_claim_correction_app import app

    at = app("installed")
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Recorded now: claim #3 (the active version)" in text
    assert "A stored claim is never edited." in text
    assert "nothing is undone or deleted" in text


# 4. Glossary

def glossary_text():
    return " ".join(f"{term} {definition}" for terms in GLOSSARY.values() for term, definition in terms)


def test_glossary_matches_implemented_vocabularies():
    from src import claim_reconciliation as reconciliation
    from src.claim_subjects import STATUSES
    from src.source_assessments import RELATIONSHIPS
    from assessment_editor import ASSESSMENTS

    text = glossary_text()
    statuses = [reconciliation.SINGLE_CLAIM, reconciliation.REPEATED_SOURCE_CLAIMS,
                reconciliation.DUPLICATE_DOCUMENT_CLAIMS, reconciliation.CONSISTENT_RELATED_SOURCES,
                reconciliation.INDEPENDENCE_UNVERIFIED, reconciliation.CONSISTENT_SOURCES,
                reconciliation.CONFLICTING_INDEPENDENCE_ASSESSMENTS, reconciliation.UNRESOLVED_CONFLICT,
                reconciliation.PERIOD_MISMATCH]
    for word in (*statuses, *STATUSES, *RELATIONSHIPS, *ASSESSMENTS, "CONSISTENT", "DIFFERS",
                 "SINGLE_OBSERVATION", "NOT_STATED", "NOT_CAPTURED", "INSUFFICIENT_EVIDENCE",
                 "INTACT", "CURRENT", "STALE", "UNKNOWN", "Quarantine", "Accounting consolidation"):
        assert word in text, word


def test_glossary_never_overstates():
    text = glossary_text().lower()
    for claim in ("proves", "proof of ownership", "verified ownership", "confirms control",
                  "establishes that a change"):
        assert claim not in text
    assert "only status crie counts as corroborated" in text
    assert "never corroborate" in text and "not evidence of direct ownership" in text


def test_glossary_renders():
    at = AppTest.from_string(
        "import sys\nsys.path[:0] = [{0!r}, {0!r} + '/app']\nimport glossary\nglossary.render_glossary()\n"
        .format(str(ROOT)), default_timeout=30).run()
    assert not at.exception
    assert at.expander[0].label == "Glossary"
    assert any("Data valid as of" in m.value for m in at.markdown)
