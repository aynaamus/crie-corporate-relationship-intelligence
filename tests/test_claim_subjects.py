
"""
Claim-subject identity mappings (Milestone 23B.2A): validation, the
recording transaction and the identity summary. Except for the two
public GLEIF LEIs of the SPP/EPI case, all identities are synthetic.
"""

from unittest.mock import MagicMock

import pytest
from mysql.connector import errors

from src.claim_subjects import (
    MappingError,
    lei_is_valid,
    prepare_mapping,
    record_mapping,
    subject_identities,
)

SPP = "097900BIBZ0000124550"   # SPP Infrastructure, a. s. (public GLEIF record)
EPI = "315700I4J1M1NKGWWY89"   # EP Infrastructure, a.s. (public GLEIF record)


def synthetic_lei(prefix):
    """A well-formed synthetic LEI with valid check digits."""
    base = (prefix + "0" * 18)[:18]
    check = 98 - int("".join(str(int(c, 36)) for c in base + "00")) % 97
    return f"{base}{check:02d}"


A, B = synthetic_lei("SYNTHA"), synthetic_lei("SYNTHB")


def mapped(**overrides):
    fields = dict(subject_text="SPP Infrastructure", status="MAPPED", basis="DOCUMENTED",
                  rationale="Synthetic: legal name matches the GLEIF record",
                  recorded_by="synthetic reviewer", lei=SPP, name_type="TRADING_NAME",
                  evidence_reference="GLEIF LEI record", evidence_location="Entity/LegalName",
                  limitations="Short form of the legal name")
    fields.update(overrides)
    return fields


# Validation

def test_lei_check_digits():
    assert lei_is_valid(SPP) and lei_is_valid(EPI) and lei_is_valid(A)
    for invalid in (SPP[:-1] + "1", SPP.lower(), SPP + "0", "", None, "097900BIBZ000012455O"):
        assert not lei_is_valid(invalid)


def test_subject_text_is_kept_exactly():
    for subject in ("SPP Infrastructure ", "spp infrastructure", "Café Holdings",
                    "Café Holdings"):
        assert prepare_mapping(**mapped(subject_text=subject))["subject_text"] == subject
    keys = {prepare_mapping(**mapped(subject_text=s))["submission_key"]
            for s in ("Café", "Café", "CAFÉ")}
    assert len(keys) == 3


@pytest.mark.parametrize("overrides, message", [
    ({"subject_text": "   "}, "Subject text is required"),
    ({"lei": SPP[:-1] + "1"}, "not a valid LEI"),
    ({"lei": None}, "not a valid LEI"),
    ({"name_type": None}, "needs the name type"),
    ({"limitations": None}, "limitations stated"),
    ({"evidence_reference": None}, "registered evidence source or an authoritative"),
    ({"evidence_location": " "}, "evidence location"),
    ({"status": "PROBABLY"}, "Unknown status"),
    ({"candidate_leis": [A, B]}, "Only an ambiguous mapping lists candidates"),
])
def test_confirmed_mapping_requirements(overrides, message):
    with pytest.raises(MappingError, match=message):
        prepare_mapping(**mapped(**overrides))


def test_ambiguous_needs_two_valid_candidates_and_no_chosen_lei():
    record = prepare_mapping(**mapped(status="AMBIGUOUS", lei=None, name_type=None,
                                      candidate_leis=[B, A, A]))
    assert record["lei"] is None and record["candidate_leis"] == f'["{A}", "{B}"]'

    with pytest.raises(MappingError, match="at least two candidate"):
        prepare_mapping(**mapped(status="AMBIGUOUS", lei=None, name_type=None, candidate_leis=[A]))
    with pytest.raises(MappingError, match="must not name a single LEI"):
        prepare_mapping(**mapped(status="AMBIGUOUS", candidate_leis=[A, B]))


def test_no_lei_never_invents_one_and_needs_search_evidence():
    record = prepare_mapping(**mapped(status="NO_LEI", lei=None, name_type=None, limitations=None,
                                      evidence_reference="Synthetic: GLEIF search, no result"))
    assert record["lei"] is None

    with pytest.raises(MappingError, match="must not name a single LEI"):
        prepare_mapping(**mapped(status="NO_LEI"))
    with pytest.raises(MappingError, match="registered evidence source"):
        prepare_mapping(**mapped(status="NO_LEI", lei=None, name_type=None,
                                 evidence_reference=None))


def test_unresolved_needs_only_a_rationale():
    record = prepare_mapping(subject_text="Synthetic Group", status="UNRESOLVED",
                             basis="INVESTIGATOR_JUDGEMENT", rationale="Not yet researched",
                             recorded_by="synthetic reviewer")
    assert record["lei"] is None and record["candidate_leis"] is None


# Recording transaction (mocked database)

def connection_with(*results, in_transaction=False):
    connection = MagicMock()
    connection.in_transaction = in_transaction
    cursor = connection.cursor.return_value
    cursor.fetchall.side_effect = [r for r in results if isinstance(r, list)]
    cursor.fetchone.side_effect = [r for r in results if not isinstance(r, list)]
    cursor.lastrowid = 41
    return connection, cursor


CASE_ROW, CLAIM_ROW = {"case_id": 1}, {"x": 1}


def test_open_caller_transaction_refused_untouched():
    connection, _ = connection_with(in_transaction=True)

    with pytest.raises(MappingError, match="open transaction"):
        record_mapping(connection, 1, **mapped())
    connection.cursor.assert_not_called()


def test_subject_must_appear_exactly_in_the_case_claims():
    connection, cursor = connection_with(CASE_ROW, None)

    with pytest.raises(MappingError, match="uses exactly that subject"):
        record_mapping(connection, 1, **mapped())

    claim_query = cursor.execute.call_args_list[1].args[0]
    assert "COLLATE utf8mb4_0900_bin" in claim_query and "s.case_id = %s" in claim_query
    connection.commit.assert_not_called()


def test_lei_must_be_a_recorded_legal_entity():
    connection, _ = connection_with(CASE_ROW, CLAIM_ROW, [])

    with pytest.raises(MappingError, match="Not a recorded legal entity"):
        record_mapping(connection, 1, **mapped())
    connection.rollback.assert_called_once()


def test_successful_and_idempotent_recording():
    connection, cursor = connection_with(CASE_ROW, CLAIM_ROW, [{"lei": SPP}], None, [])
    assert record_mapping(connection, 1, **mapped()) == {"mapping_id": 41, "created": True}
    connection.commit.assert_called_once()

    connection, cursor = connection_with(CASE_ROW, CLAIM_ROW, [{"lei": SPP}], {"mapping_id": 7})
    assert record_mapping(connection, 1, **mapped()) == {"mapping_id": 7, "created": False}
    assert not any("INSERT INTO" in c.args[0] for c in cursor.execute.call_args_list)


@pytest.mark.parametrize("supersedes, current, message", [
    (None, [{"mapping_id": 9}], "Mapping 9 is current"),
    (8, [{"mapping_id": 9}], "Mapping 9 is current"),
    (8, [], "not the current one"),
])
def test_reassignment_must_supersede_the_current_mapping(supersedes, current, message):
    connection, _ = connection_with(CASE_ROW, CLAIM_ROW, [{"lei": SPP}], None, current)

    with pytest.raises(MappingError, match=message):
        record_mapping(connection, 1, **mapped(supersedes_mapping_id=supersedes))
    connection.commit.assert_not_called()


@pytest.mark.parametrize("errno, expected", [(1146, RuntimeError), (1062, MappingError),
                                             (1644, MappingError), (1452, MappingError),
                                             (3819, MappingError)])
def test_database_rejections_save_nothing(errno, expected):
    connection, cursor = connection_with(CASE_ROW, CLAIM_ROW, [{"lei": SPP}], None, [])

    def execute(sql, params=None):
        if "INSERT INTO" in sql:
            raise errors.DatabaseError(msg="synthetic", errno=errno)

    cursor.execute.side_effect = execute
    with pytest.raises(expected):
        record_mapping(connection, 1, **mapped())
    connection.commit.assert_not_called()


# Identity summary (pure)

def row(i, subject, status="MAPPED", lei=SPP, supersedes=None, candidates=None):
    return {"mapping_id": i, "subject_text": subject, "status": status, "lei": lei,
            "candidate_leis": candidates, "supersedes_mapping_id": supersedes}


def by_subject(result):
    return {s["subject_text"]: s for s in result["subjects"]}


def test_unmapped_ambiguous_and_mapped_subjects():
    result = by_subject(subject_identities(
        ["SPP Infrastructure", "Synthetic Group", "Synthetic Holdings"],
        [row(1, "SPP Infrastructure"),
         row(2, "Synthetic Group", "AMBIGUOUS", None, candidates=f'["{A}", "{B}"]')],
        case_parties=(SPP, EPI)))

    assert result["SPP Infrastructure"]["status"] == "MAPPED" and not result["SPP Infrastructure"]["notes"]
    assert result["Synthetic Group"]["status"] == "AMBIGUOUS"
    assert result["Synthetic Group"]["lei"] is None and result["Synthetic Group"]["candidate_leis"] == [A, B]
    assert result["Synthetic Holdings"]["status"] == "UNMAPPED"


def test_diagnostics_point_to_review_without_changing_anything():
    result = subject_identities(
        ["SPP Infrastructure", "SPPI", "Synthetic Holdings"],
        [row(1, "SPP Infrastructure", lei=A), row(2, "SPP Infrastructure", supersedes=1),
         row(3, "SPPI"), row(4, "Synthetic Holdings", lei=B), row(5, "Gone Subject", lei=B)],
        case_parties=(SPP, EPI))
    subjects = by_subject(result)

    assert any("reassigned" in n for n in subjects["SPP Infrastructure"]["notes"])
    assert any("also mapped from: SPPI" in n for n in subjects["SPP Infrastructure"]["notes"])
    assert any("not a party" in n for n in subjects["Synthetic Holdings"]["notes"])
    assert result["orphaned_mappings"] == ["Gone Subject"]
    assert subjects["SPP Infrastructure"]["mapping_id"] == 2


def test_case_variant_subjects_are_different_subjects():
    result = by_subject(subject_identities(["SPP Infrastructure", "SPP INFRASTRUCTURE"],
                                           [row(1, "SPP Infrastructure")]))

    assert result["SPP Infrastructure"]["status"] == "MAPPED"
    assert result["SPP INFRASTRUCTURE"]["status"] == "UNMAPPED"
