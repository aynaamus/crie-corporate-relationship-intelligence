
import datetime
from unittest.mock import MagicMock

import pytest

import src.evidence_snapshot as snap
from src.evidence_snapshot import (
    canonical_json,
    load_evidence_snapshot,
    snapshot_sha256,
)


UTC_NOW = datetime.datetime(2026, 2, 1, 12, 0, 0, 123456)


def history(history_id, source_id, new, previous=None):
    return {
        "history_id": history_id,
        "source_id": source_id,
        "event_type": "REVIEW",
        "previous_assessment": previous,
        "new_assessment": new,
        "review_reason": "Fictional review",
        "recorded_at_utc": UTC_NOW,
    }


def db_rows(assessment="SUPPORTS", history_rows=None,
            annotations=(), corrections=(), lineage_columns=(),
            assessments_table=False):
    """
    Result sets in the order _read() issues its queries. The
    historical-claims query runs only when corrections refer to
    claims that are not current; append its result set then.
    """

    return [
        [{"case_id": 7}],
        [{"captured_at_utc": UTC_NOW}],
        [{
            "source_id": 3,
            "document_title": "Fictional Report",
            "publisher": None,
            "source_url": "https://example.org/r.pdf",
            "publication_date": datetime.date(2025, 4, 1),
            "evidence_assessment": assessment,
            "added_at": UTC_NOW,
        }],
        list(history_rows if history_rows is not None
             else [history(1, 3, "SUPPORTS")]),
        list(annotations),
        [],
        [{
            "claim_id": 41,
            "claim_fingerprint": "f" * 64,
            "source_id": 3,
            "entity_name": "Northwind Holdings plc",
            "metric": "equity_interest_pct",
            "report_year": 2025,
            "period_end": datetime.date(2025, 9, 30),
            "value_text": "49%",
            "normalized_value": "49",
            "source_location": "Page 4",
            "created_at": UTC_NOW,
        }],
        [{"TABLE_NAME": "claim_corrections", "COLUMN_NAME": name}
         for name in lineage_columns]
        + ([{"TABLE_NAME": "source_relationship_assessments",
             "COLUMN_NAME": "assessment_id"}] if assessments_table else []),
        list(corrections),
    ]


def connection_with(result_sets, in_transaction=True, settings=None):
    connection = MagicMock()
    connection.in_transaction = in_transaction
    cursor = connection.cursor.return_value

    prefix = [[settings]] if settings else []
    cursor.fetchall.side_effect = prefix + result_sets

    return connection, cursor


UTC_SETTINGS = {"isolation": "REPEATABLE-READ", "time_zone": "+00:00"}


def test_owned_connection_uses_consistent_read_only_snapshot(monkeypatch):
    connection, cursor = connection_with(db_rows())
    monkeypatch.setattr(snap, "_connect", lambda: connection)

    snapshot = load_evidence_snapshot(case_id=7)

    first_sql = cursor.execute.call_args_list[0].args[0]
    assert first_sql == "SET SESSION time_zone = '+00:00'"

    connection.start_transaction.assert_called_once_with(
        consistent_snapshot=True,
        isolation_level="REPEATABLE READ",
        readonly=True,
    )
    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()
    connection.close.assert_called_once()

    claim = snapshot["claims"][0]
    assert claim["claim_id"] == 41
    assert claim["period_end"] == "2025-09-30"
    assert claim["created_at_utc"] == "2026-02-01T12:00:00.123456Z"

    source = snapshot["sources"][0]
    assert source["publisher"] is None
    assert source["publication_date"] == "2025-04-01"
    assert source["latest_assessment_history_id"] == 1


def test_owned_connection_closed_on_failure(monkeypatch):
    connection, cursor = connection_with([])
    cursor.execute.side_effect = [None, RuntimeError("query failed")]
    monkeypatch.setattr(snap, "_connect", lambda: connection)

    with pytest.raises(RuntimeError, match="query failed"):
        load_evidence_snapshot(case_id=7)

    connection.rollback.assert_called_once()
    connection.close.assert_called_once()


def test_caller_connection_must_be_in_transaction():
    connection, _ = connection_with(db_rows(), in_transaction=False)

    with pytest.raises(ValueError, match="consistent-snapshot transaction"):
        load_evidence_snapshot(case_id=7, connection=connection)


@pytest.mark.parametrize(
    "settings,message",
    [
        ({"isolation": "READ-COMMITTED", "time_zone": "+00:00"},
         "REPEATABLE READ"),
        ({"isolation": "REPEATABLE-READ", "time_zone": "SYSTEM"},
         "UTC session"),
    ],
)
def test_caller_connection_settings_checked(settings, message):
    connection, _ = connection_with(db_rows(), settings=settings)

    with pytest.raises(ValueError, match=message):
        load_evidence_snapshot(case_id=7, connection=connection)


def test_caller_connection_untouched():
    connection, _ = connection_with(db_rows(), settings=UTC_SETTINGS)

    load_evidence_snapshot(case_id=7, connection=connection)

    connection.start_transaction.assert_not_called()
    connection.commit.assert_not_called()
    connection.rollback.assert_not_called()
    connection.close.assert_not_called()


def test_case_not_found():
    rows = db_rows()
    rows[0] = []
    connection, _ = connection_with(rows, settings=UTC_SETTINGS)

    with pytest.raises(ValueError, match="case 7 not found"):
        load_evidence_snapshot(case_id=7, connection=connection)


def test_assessment_inconsistent_with_history_fails():
    connection, _ = connection_with(
        db_rows(assessment="CONTRADICTS"), settings=UTC_SETTINGS
    )

    with pytest.raises(ValueError, match="does not match history"):
        load_evidence_snapshot(case_id=7, connection=connection)


def test_accidental_duplicates_not_cited():
    rows = db_rows(
        history_rows=[
            history(1, 3, "SUPPORTS", "NOT_REVIEWED"),
            history(2, 3, "SUPPORTS", "SUPPORTS"),
        ],
        annotations=[{
            "annotation_id": 1,
            "history_id": 2,
            "annotation_type": "ACCIDENTAL_DUPLICATE",
            "annotation_note": "Repeated submission",
            "created_at_utc": UTC_NOW,
        }],
    )
    connection, _ = connection_with(rows, settings=UTC_SETTINGS)

    source = load_evidence_snapshot(
        case_id=7, connection=connection
    )["sources"][0]

    assert source["latest_assessment_history_id"] == 1
    # The annotated record is retained, not hidden.
    assert [h["history_id"] for h in source["assessment_history"]] == [1, 2]
    assert source["assessment_history"][1]["annotations"][0][
        "annotation_type"
    ] == "ACCIDENTAL_DUPLICATE"


def test_stored_normalization_mismatch_fails():
    rows = db_rows()
    rows[6][0]["normalized_value"] = "49%"
    connection, _ = connection_with(rows, settings=UTC_SETTINGS)

    with pytest.raises(ValueError, match="normalized"):
        load_evidence_snapshot(case_id=7, connection=connection)


def test_no_current_claims_fails():
    rows = db_rows()
    rows[6] = []
    connection, _ = connection_with(rows, settings=UTC_SETTINGS)

    with pytest.raises(ValueError, match="No current disclosure claims"):
        load_evidence_snapshot(case_id=7, connection=connection)


def test_canonical_json_is_order_independent():
    assert canonical_json({"b": 1, "a": "é"}) == canonical_json(
        {"a": "é", "b": 1}
    )
    assert canonical_json({"a": "é"}) == '{"a":"é"}'.encode("utf-8")

    with pytest.raises(ValueError):
        canonical_json({"a": float("nan")})


def test_snapshot_hash_ignores_capture_time_only(snapshot_tools):
    t = snapshot_tools
    base = t.snapshot([t.claim(1, 1, "49")])

    later = dict(base, captured_at_utc="2030-01-01T00:00:00.000000Z")
    assert snapshot_sha256(base) == snapshot_sha256(later)

    edited = t.snapshot([t.claim(1, 1, "51")])
    assert snapshot_sha256(base) != snapshot_sha256(edited)

    relocated = t.snapshot([t.claim(1, 1, "49", location="Page 2")])
    assert snapshot_sha256(base) != snapshot_sha256(relocated)
