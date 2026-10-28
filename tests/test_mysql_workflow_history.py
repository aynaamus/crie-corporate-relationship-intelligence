
"""The case-wide assessment history query (23B.3) against the test database (read-only)."""

import sys
from pathlib import Path

from test_mysql_evidence_snapshot import block_default_connections, connect, pytestmark  # noqa: F401
from test_mysql_report_registry import registry_db, seed  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from workflow_ui import _load_history, review_rows  # noqa: E402


def test_case_assessment_history_reads_existing_records(registry_db):
    _, fixtures, _ = registry_db
    case_id, a, b, *_ = seed(fixtures)

    reviews, relationships = _load_history(connect, case_id)

    assert sorted({r["source_id"] for r in reviews}) == [a, b]        # this case only
    rows = review_rows(reviews)
    assert {r["Status"] for r in rows} == {"Current"}
    assert {r["Assessment"] for r in rows} == {"SUPPORTS"}
    assert relationships == []                                       # installed, none recorded
