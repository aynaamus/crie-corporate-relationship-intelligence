
"""Pure identity, hashing and lifecycle rules of GLEIF observations (23B.2B-1)."""

import datetime

import pytest

from src.gleif_observations import (
    NOT_CAPTURED,
    ObservationError,
    absence_meaning,
    batch_coverage,
    batch_counts_consistent,
    canonical_selection,
    check_sha256,
    check_transition,
    classify_records,
    legacy_observation,
    record_sha256,
    selection_complete,
    selection_fingerprint,
    stated_end,
)

LEI_A, LEI_B = "SYNTHA00000000000047", "SYNTHB00000000000016"
KEY = (LEI_A, LEI_B, "IS_DIRECTLY_CONSOLIDATED_BY")


def rr(status="ACTIVE", validation="ENTITY_SUPPLIED_ONLY", **extra):
    return {"child_lei": LEI_A, "parent_lei": LEI_B,
            "relationship_type": KEY[2], "relationship_status": status,
            "registration_status": "PUBLISHED", "validation_sources": validation,
            "periods": [{"type": "ACCOUNTING_PERIOD", "start": "2023-01-01", "end": "2023-12-31"}],
            **extra}


def test_selection_fingerprint_is_deterministic_and_order_free():
    one = canonical_selection("RR", "LEI_SET", leis=[LEI_B, LEI_A.lower(), LEI_A],
                              match="CHILD_OR_PARENT")
    two = canonical_selection("RR", "LEI_SET", leis=[LEI_A, LEI_B], match="CHILD_OR_PARENT")

    assert one == two and one["leis"] == [LEI_A, LEI_B]
    assert selection_fingerprint(one) == selection_fingerprint(two)
    check_sha256(selection_fingerprint(one))


def test_distinct_selections_of_one_file_have_distinct_fingerprints():
    selections = [
        canonical_selection("RR", "ALL_RECORDS"),
        canonical_selection("RR", "FIRST_N", limit=250),
        canonical_selection("RR", "FIRST_N", limit=251),
        canonical_selection("RR", "LEI_SET", leis=[LEI_A], match="CHILD"),
        canonical_selection("RR", "LEI_SET", leis=[LEI_A], match="PARENT"),
        canonical_selection("LEI2", "ALL_RECORDS"),
    ]
    assert len({selection_fingerprint(s) for s in selections}) == len(selections)


@pytest.mark.parametrize("args", [
    ("RR", "LEI_SET", {"leis": [], "match": "CHILD"}),
    ("RR", "LEI_SET", {"leis": ["NOT-AN-LEI"], "match": "CHILD"}),
    ("RR", "LEI_SET", {"leis": [LEI_A], "match": "ENTITY"}),
    ("LEI2", "LEI_SET", {"leis": [LEI_A], "match": "CHILD"}),
    ("RR", "FIRST_N", {"limit": 0}),
    ("RR", "FIRST_N", {"limit": True}),
    ("RR", "ALL_RECORDS", {"limit": 5}),
    ("RR", "ALL_RECORDS", {"leis": [LEI_A]}),
    ("XX", "ALL_RECORDS", {}),
])
def test_invalid_selections_are_rejected(args):
    dataset, mode, extra = args
    with pytest.raises(ObservationError):
        canonical_selection(dataset, mode, **extra)


def test_record_hash_ignores_field_order_but_not_exact_text():
    base = rr()
    reordered = dict(reversed(list(base.items())))

    assert record_sha256(base) == record_sha256(reordered)
    for changed in (rr(status="INACTIVE"), rr(validation="PARTIALLY_CORROBORATED"),
                    rr(status="Active"), rr(status="ACTIVE "), rr(name="Café"),
                    rr(name="Café")):
        assert record_sha256(changed) != record_sha256(base)
    assert record_sha256(rr(name="Café")) != record_sha256(rr(name="Café"))
    # A field the record does not state is not the same as an empty one.
    assert record_sha256(rr(qualifiers=None)) != record_sha256(rr(qualifiers=[]))


def test_repeated_records_within_one_file():
    grouped = classify_records([(1, KEY, rr()), (2, KEY, rr()), (3, KEY, rr(status="INACTIVE")),
                                (4, (LEI_B, LEI_A, KEY[2]), rr())])

    same = grouped[KEY]
    assert same["conflicting"] is True
    assert [(o["source_record_ordinal"], o["identical_repeats"]) for o in same["observations"]] \
        == [(1, 1), (3, 0)]
    assert grouped[(LEI_B, LEI_A, KEY[2])]["conflicting"] is False


def test_batch_lifecycle_transitions():
    for allowed in ((None, "STARTED"), ("STARTED", "STARTED"), ("STARTED", "FAILED"),
                    ("FAILED", "STARTED"), ("STARTED", "COMPLETED")):
        check_transition(*allowed)
    for refused in ((None, "COMPLETED"), ("COMPLETED", "STARTED"), ("COMPLETED", "FAILED"),
                    ("COMPLETED", "COMPLETED"), ("FAILED", "COMPLETED"), ("FAILED", "FAILED")):
        with pytest.raises(ObservationError):
            check_transition(*refused)


def test_batch_count_invariant():
    good = {"selection_mode": "LEI_SET", "records_scanned": 10, "records_selected": 4,
            "observations_inserted": 2, "observations_already_recorded": 1,
            "identical_duplicates": 1}
    assert batch_counts_consistent(good)
    assert not batch_counts_consistent({**good, "observations_inserted": 1})
    assert not batch_counts_consistent({**good, "records_scanned": 3})
    # ALL_RECORDS and FIRST_N select every record they scan...
    assert not batch_counts_consistent({**good, "selection_mode": "ALL_RECORDS"})
    first = {**good, "selection_mode": "FIRST_N", "records_scanned": 4, "selection_limit": 4}
    assert batch_counts_consistent(first)
    # ...and FIRST_N never scans past its limit.
    assert not batch_counts_consistent({**first, "selection_limit": 3})


def test_completion_depends_on_the_selection():
    assert selection_complete("ALL_RECORDS", 10, 10)
    assert not selection_complete("ALL_RECORDS", 9, 10)
    # A matching record can be anywhere, so an LEI set reads the whole file.
    assert not selection_complete("LEI_SET", 9, 10) and selection_complete("LEI_SET", 10, 10)
    # FIRST_N reads only its prefix, or the whole file when it is shorter.
    assert selection_complete("FIRST_N", 3, 10, limit=3)
    assert not selection_complete("FIRST_N", 2, 10, limit=3)
    assert selection_complete("FIRST_N", 2, 2, limit=250)
    with pytest.raises(ObservationError):
        selection_complete("SOME", 1, 1)


def test_a_completed_sample_is_never_a_complete_import():
    assert batch_coverage("COMPLETED", "ALL_RECORDS", "FULL") == "COMPLETE_PUBLISHED_DATASET"
    assert batch_coverage("COMPLETED", "ALL_RECORDS", "DELTA") == "COMPLETE_DELTA_FILE"
    assert batch_coverage("COMPLETED", "ALL_RECORDS", "UNKNOWN") == "COMPLETE_FILE_SCOPE_UNKNOWN"
    for mode in ("LEI_SET", "FIRST_N"):
        assert batch_coverage("COMPLETED", mode, "FULL") == "COMPLETE_SAMPLE_OF_FILE"
    for status in ("STARTED", "FAILED"):
        assert batch_coverage(status, "ALL_RECORDS", "FULL") == "INCOMPLETE_IMPORT"


def test_absence_is_never_a_termination():
    every = canonical_selection("RR", "ALL_RECORDS")
    sample = canonical_selection("RR", "FIRST_N", limit=250)
    chosen = canonical_selection("RR", "LEI_SET", leis=[LEI_A], match="CHILD")

    assert absence_meaning("FULL", sample) == "ABSENT_FROM_SAMPLE"
    assert absence_meaning("FULL", chosen, subject_in_selection=False) == "ABSENT_FROM_SAMPLE"
    assert absence_meaning("DELTA", every) == "ABSENT_FROM_DELTA"
    assert absence_meaning("UNKNOWN", every) == "ABSENT_SCOPE_UNKNOWN"
    assert absence_meaning("FULL", every) == "NOT_PUBLISHED_IN_FULL_DATASET"
    assert absence_meaning("FULL", chosen) == "NOT_PUBLISHED_IN_FULL_DATASET"
    meanings = {absence_meaning(s, every) for s in ("FULL", "DELTA", "UNKNOWN")}
    assert not any("END" in m or "TERMINAT" in m for m in meanings)


def test_reported_inactive_or_retired_is_a_statement_not_an_established_end():
    assert stated_end(rr()) is None
    assert stated_end(rr(status="INACTIVE")) == "REPORTED_INACTIVE"
    assert stated_end({**rr(), "registration_status": "RETIRED"}) == "REPORTED_RETIRED"
    assert stated_end({"relationship_status": None}) is None


def test_legacy_anchor_reports_uncaptured_fields_and_borrows_nothing():
    anchor = {"child_lei": LEI_A, "parent_lei": LEI_B, "relationship_type": KEY[2],
              "snapshot_at_utc": datetime.datetime(2026, 10, 22, 9, 0, 1),
              "source_file": "20261022-gleif-concatenated-file-rr.xml",
              "validation_sources": "ENTITY_SUPPLIED_ONLY",
              "validation_documents": "OTHER_OFFICIAL_DOCUMENTS",
              "first_batch_id": None,
              # The identity table's first-seen status must not be borrowed.
              "relationship_status": "ACTIVE", "registration_status": "PUBLISHED"}
    legacy = legacy_observation(anchor)

    assert legacy["origin"] == "LEGACY_IMPORT"
    for field in ("relationship_status", "registration_status", "qualifiers", "quantifiers"):
        assert legacy[field] == NOT_CAPTURED
    assert legacy["validation_sources"] == "ENTITY_SUPPLIED_ONLY"
    assert legacy["publication_time_utc"] == anchor["snapshot_at_utc"]
    with pytest.raises(ObservationError):
        legacy_observation({**anchor, "first_batch_id": 7})


def test_legacy_view_names_content_date_as_data_valid_as_of():
    anchor = {"child_lei": LEI_A, "parent_lei": LEI_B, "relationship_type": KEY[2],
              "snapshot_at_utc": datetime.datetime(2026, 10, 22, 9, 0, 1), "source_file": "x.xml",
              "validation_sources": None, "validation_documents": None, "first_batch_id": None}
    legacy = legacy_observation(anchor)
    assert legacy["data_valid_as_of_utc"] == anchor["snapshot_at_utc"]
    assert legacy["publication_time_utc"] == legacy["data_valid_as_of_utc"]   # deprecated alias


@pytest.mark.parametrize("value", [1.5, float("nan"), (1, 2), {1: "x"}, b"x"])
def test_canonical_records_refuse_values_json_cannot_round_trip(value):
    with pytest.raises(ObservationError, match="cannot hold"):
        record_sha256({"field": value})


def test_coverage_properties_are_independent():
    from src.gleif_observations import coverage_properties

    trusted = coverage_properties("COMPLETED", "ALL_RECORDS", "FULL")
    assert trusted["trusted_complete_dataset"] and trusted["label"] == "COMPLETE_PUBLISHED_DATASET"

    quarantined = coverage_properties("COMPLETED", "ALL_RECORDS", "FULL", quarantined_records=3)
    assert quarantined["integrity"] == "QUARANTINE_PRESENT" and quarantined["quarantined_records"] == 3
    assert not quarantined["trusted_complete_dataset"]
    assert quarantined["label"] == "COMPLETE_FILE_SCAN_WITH_QUARANTINED_RECORDS"

    sample = coverage_properties("COMPLETED", "FIRST_N", "FULL", quarantined_records=2)
    assert sample["selection_is_sample"] and sample["label"] == "COMPLETE_SAMPLE_OF_FILE"
    assert sample["integrity"] == "QUARANTINE_PRESENT" and not sample["trusted_complete_dataset"]

    delta = coverage_properties("COMPLETED", "ALL_RECORDS", "DELTA")
    assert delta["source_scope"] == "DELTA" and not delta["trusted_complete_dataset"]
    for status in ("STARTED", "FAILED"):
        assert coverage_properties(status, "ALL_RECORDS", "FULL")["label"] == "INCOMPLETE_IMPORT"
    with pytest.raises(ObservationError):
        coverage_properties("COMPLETED", "ALL_RECORDS", "FULL", quarantined_records=-1)


def test_quarantine_makes_absence_undetermined():
    every = canonical_selection("RR", "ALL_RECORDS")
    assert absence_meaning("FULL", every) == "NOT_PUBLISHED_IN_FULL_DATASET"
    assert absence_meaning("FULL", every, quarantined_records=1) == \
        "ABSENCE_UNDETERMINED_QUARANTINE_PRESENT"
    sample = canonical_selection("RR", "FIRST_N", limit=2)
    assert absence_meaning("FULL", sample, quarantined_records=1) == "ABSENT_FROM_SAMPLE"
