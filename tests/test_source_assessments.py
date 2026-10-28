
"""
Source-relationship assessments (Milestone 23B.1): validation, the
pair-evaluation rules and the recording transaction. All evidence
here is synthetic.
"""

from unittest.mock import MagicMock

import pytest
from mysql.connector import errors

from src.source_assessments import (
    DEPENDENT,
    AssessmentError,
    current_assessments,
    evaluate_pair,
    prepare_assessment,
    record_assessment,
)

DISCLOSURE = {"entity_name": "Fictional Holdings", "metric": "interest_pct",
              "report_year": 2025, "period_end": "2025-12-31"}
KEY = ("Fictional Holdings", "interest_pct", "2025", "2025-12-31")


def independent(**overrides):
    fields = dict(source_a_id=2, source_b_id=1, relationship="INDEPENDENT_ORIGIN",
                  basis="DOCUMENTED", rationale="Synthetic: separate audited filings",
                  recorded_by="synthetic reviewer", limitations="Synthetic limitation",
                  evidence_reference="Synthetic register entry 42",
                  evidence_location="Entry 42", disclosure=DISCLOSURE)
    fields.update(overrides)
    return fields


# Validation

def test_pairs_are_canonical_and_keys_order_independent():
    a = prepare_assessment(**independent())
    b = prepare_assessment(**independent(source_a_id=1, source_b_id=2))

    assert (a["source_a_id"], a["source_b_id"]) == (1, 2)
    assert a["submission_key"] == b["submission_key"]


@pytest.mark.parametrize("overrides, message", [
    ({"source_b_id": 2}, "two different sources"),
    ({"relationship": "PROBABLY_INDEPENDENT"}, "Unknown relationship"),
    ({"basis": "HUNCH"}, "Unknown basis"),
    ({"rationale": "  "}, "Rationale is required"),
    ({"recorded_by": ""}, "Reviewer is required"),
    ({"disclosure": {**DISCLOSURE, "period_end": None}}, "needs entity, metric"),
])
def test_invalid_assessments_rejected(overrides, message):
    with pytest.raises(AssessmentError, match=message):
        prepare_assessment(**independent(**overrides))


@pytest.mark.parametrize("overrides, message", [
    ({"evidence_reference": None}, "registered evidence source or an external"),
    ({"evidence_location": " "}, "evidence location"),
    ({"limitations": None}, "limitations stated"),
])
def test_independence_needs_traceable_evidence_not_rationale_alone(overrides, message):
    with pytest.raises(AssessmentError, match=message):
        prepare_assessment(**independent(**overrides))


def test_registered_evidence_source_is_enough_reference():
    record = prepare_assessment(**independent(evidence_reference=None, evidence_source_id=3))

    assert record["evidence_source_id"] == 3


def test_dependence_and_unknown_need_no_evidence_reference():
    for relationship in (*DEPENDENT, "UNKNOWN"):
        record = prepare_assessment(**independent(
            relationship=relationship, evidence_reference=None, evidence_location=None,
            limitations=None, disclosure=None))
        assert record["metric"] is None and record["relationship"] == relationship


# Pair evaluation

def assessment(i, relationship, scope=KEY, basis="DOCUMENTED", supersedes=None):
    fields = dict(zip(("entity_name", "metric", "report_year", "period_end"),
                      scope if scope else (None,) * 4))
    return {"assessment_id": i, "source_a_id": 1, "source_b_id": 2,
            "relationship": relationship, "basis": basis,
            "rationale": f"Synthetic rationale {i}", "supersedes_assessment_id": supersedes,
            **fields}


@pytest.mark.parametrize("relationship", DEPENDENT)
def test_every_dependence_blocks(relationship):
    assert evaluate_pair([assessment(1, relationship)], KEY)["outcome"] == "DEPENDENT"
    # Pair-wide dependence applies to every disclosure.
    assert evaluate_pair([assessment(1, relationship, scope=None)], KEY)["outcome"] == "DEPENDENT"


def test_disclosure_scoped_independence_qualifies_only_its_disclosure():
    rows = [assessment(1, "INDEPENDENT_ORIGIN")]

    assert evaluate_pair(rows, KEY)["outcome"] == "INDEPENDENT"
    other_period = ("Fictional Holdings", "interest_pct", "2025", "2025-09-30")
    other_metric = ("Fictional Holdings", "revenue", "2025", "2025-12-31")
    assert evaluate_pair(rows, other_period)["outcome"] == "UNVERIFIED"
    assert evaluate_pair(rows, other_metric)["outcome"] == "UNVERIFIED"


def test_pair_wide_independence_does_not_imply_claim_independence():
    result = evaluate_pair([assessment(1, "INDEPENDENT_ORIGIN", scope=None)], KEY)

    assert result["outcome"] == "UNVERIFIED"
    assert [c["assessment_id"] for c in result["pair_level_independence_not_applied"]] == [1]


def test_independent_for_one_metric_dependent_for_another():
    other = ("Fictional Holdings", "revenue", "2025", "2025-12-31")
    rows = [assessment(1, "INDEPENDENT_ORIGIN"), assessment(2, "QUOTES_OR_ATTRIBUTES", scope=other)]

    assert evaluate_pair(rows, KEY)["outcome"] == "INDEPENDENT"
    assert evaluate_pair(rows, other)["outcome"] == "DEPENDENT"


@pytest.mark.parametrize("rows, automatic", [
    ([assessment(1, "INDEPENDENT_ORIGIN"), assessment(2, "SAME_CORPORATE_GROUP", scope=None)], None),
    ([assessment(1, "INDEPENDENT_ORIGIN")], "SHARED_PUBLISHER"),
    ([assessment(1, "INDEPENDENT_ORIGIN")], "APPARENT_DUPLICATE"),
])
def test_contradictions_are_conflicts_with_their_evidence(rows, automatic):
    result = evaluate_pair(rows, KEY, automatic)

    assert result["outcome"] == "CONFLICT"
    assert [c["assessment_id"] for c in result["independence"]] == [1]
    assert all(c["rationale"] for c in result["independence"] + result["dependence"])


def test_unknown_and_absent_stay_unverified():
    assert evaluate_pair([assessment(1, "UNKNOWN")], KEY)["outcome"] == "UNVERIFIED"
    assert evaluate_pair([], KEY)["outcome"] == "UNVERIFIED"


def test_superseded_assessments_do_not_decide():
    rows = [assessment(1, "INDEPENDENT_ORIGIN"), assessment(2, "UNKNOWN", supersedes=1)]

    assert [a["assessment_id"] for a in current_assessments(rows)] == [2]
    assert evaluate_pair(current_assessments(rows), KEY)["outcome"] == "UNVERIFIED"


# Recording transaction (mocked database)

def connection_with(*results, in_transaction=False):
    connection = MagicMock()
    connection.in_transaction = in_transaction
    cursor = connection.cursor.return_value
    cursor.fetchall.side_effect = [r for r in results if isinstance(r, list)]
    cursor.fetchone.side_effect = [r for r in results if not isinstance(r, list)]
    cursor.lastrowid = 77
    return connection, cursor


def test_open_caller_transaction_refused_untouched():
    connection, _ = connection_with(in_transaction=True)

    with pytest.raises(AssessmentError, match="open transaction"):
        record_assessment(connection, 1, **independent())

    connection.cursor.assert_not_called()


def test_sources_must_belong_to_the_case():
    connection, _ = connection_with([{"source_id": 1}])

    with pytest.raises(AssessmentError, match="registered in this investigation"):
        record_assessment(connection, 1, **independent())

    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()


def test_successful_recording():
    # Both sources, the disclosure exists, no duplicate, no current row.
    connection, cursor = connection_with([{"source_id": 1}, {"source_id": 2}], {"x": 1}, None, [])

    result = record_assessment(connection, 1, **independent())

    assert result == {"assessment_id": 77, "created": True}
    connection.commit.assert_called_once()
    insert = [c for c in cursor.execute.call_args_list if "INSERT INTO" in c.args[0]]
    assert len(insert) == 1


def test_identical_resubmission_returns_existing():
    connection, cursor = connection_with([{"source_id": 1}, {"source_id": 2}], {"x": 1},
                                         {"assessment_id": 5})

    assert record_assessment(connection, 1, **independent()) == {"assessment_id": 5, "created": False}
    assert not any("INSERT INTO" in c.args[0] for c in cursor.execute.call_args_list)
    connection.commit.assert_not_called()


@pytest.mark.parametrize("supersedes, current, message", [
    (None, [{"assessment_id": 9}], "Assessment 9 is current"),
    (8, [{"assessment_id": 9}], "Assessment 9 is current"),
    (8, [], "not the current one"),
])
def test_supersession_must_name_the_current_assessment(supersedes, current, message):
    connection, _ = connection_with([{"source_id": 1}, {"source_id": 2}], {"x": 1}, None, current)

    with pytest.raises(AssessmentError, match=message):
        record_assessment(connection, 1, **independent(supersedes_assessment_id=supersedes))

    connection.commit.assert_not_called()


@pytest.mark.parametrize("errno, expected", [(1146, RuntimeError), (1062, AssessmentError),
                                             (1644, AssessmentError), (1452, AssessmentError),
                                             (3819, AssessmentError)])
def test_database_rejections_save_nothing(errno, expected):
    connection, cursor = connection_with([{"source_id": 1}, {"source_id": 2}], {"x": 1}, None, [])

    def execute(sql, params=None):
        if "INSERT INTO" in sql:
            raise errors.DatabaseError(msg="synthetic", errno=errno)

    cursor.execute.side_effect = execute

    with pytest.raises(expected):
        record_assessment(connection, 1, **independent())

    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()
