
"""
Snapshot version 3 (Milestone 23B.1): recorded source-relationship
assessments in reconciliation, snapshots, reports and currency. All
sources, claims and assessments are synthetic.
"""

import copy
import datetime
import json

import pytest

import src.evidence_snapshot as snap
from src.evidence_snapshot import as_version, snapshot_sha256
from src.generate_versioned_report import generate_versioned_report
from src.report_registry import ManifestError, prepare_run, validate_manifest
from src.report_renderer import analyze_snapshot
from src.report_verification import check_currency

T = datetime.datetime(2026, 2, 1, 12, 0, 0, 123456)
NOW = datetime.datetime(2026, 2, 1, 12, 0, tzinfo=datetime.timezone.utc)
CODE = {"git_commit": "a" * 40, "working_tree_dirty": False}
D25 = datetime.date(2025, 12, 31)


def source(source_id, publisher):
    return {"source_id": source_id, "document_title": f"Synthetic report {source_id}",
            "publisher": publisher, "source_url": f"https://synthetic-{source_id}.example/r.pdf",
            "publication_date": None, "evidence_assessment": "NOT_REVIEWED", "added_at": T}


def claim(claim_id, source_id, value="49", metric="interest_pct", period=D25, year=2025):
    return {"claim_id": claim_id, "claim_fingerprint": f"{claim_id:064x}", "source_id": source_id,
            "entity_name": "Synthetic Holdings", "metric": metric, "report_year": year,
            "period_end": period, "value_text": value, "normalized_value": value,
            "source_location": "Page 1", "created_at": T}


def assessment(i, a, b, relationship, metric="interest_pct", period=D25, supersedes=None,
               scoped=True):
    return {"assessment_id": i, "source_a_id": a, "source_b_id": b,
            "entity_name": "Synthetic Holdings" if scoped else None,
            "metric": metric if scoped else None, "report_year": 2025 if scoped else None,
            "period_end": period if scoped else None, "relationship": relationship,
            "basis": "DOCUMENTED", "evidence_source_id": None,
            "evidence_reference": "Synthetic evidence", "evidence_location": "Synthetic page",
            "rationale": f"Synthetic rationale {i}", "limitations": "Synthetic limitation",
            "recorded_by": "synthetic reviewer", "recorded_at_utc": T,
            "supersedes_assessment_id": supersedes}


def rows(claims, assessments=(), schema="INSTALLED", publishers=("A plc", "B ltd", "C sa")):
    return {
        "captured": [{"captured_at_utc": T}],
        "sources": [source(i + 1, p) for i, p in enumerate(publishers)],
        "history": [], "annotations": [], "metadata_changes": [],
        "claims": list(claims), "corrections": [], "historical_claims": [],
        "lineage_schema": "INSTALLED", "source_assessments": list(assessments),
        "source_assessments_schema": schema,
    }


def v3(*claims, assessments=(), **kwargs):
    return snap._assemble(7, (2024, 2025), rows(claims, assessments, **kwargs))


def reconciliation(snapshot):
    return {(r["metric"], r["period_end"]): r
            for r in analyze_snapshot(snapshot, 2024, 2025)["reconciliation"]}


AGREE = (claim(1, 1), claim(2, 2))


# Reconciliation outcomes

def test_without_assessments_agreement_is_unverified():
    (row,) = reconciliation(v3(*AGREE)).values()

    assert row["reconciliation_status"] == "INDEPENDENCE_UNVERIFIED"
    details = json.loads(row["independence"])
    assert [p["source_ids"] for p in details["unverified_pairs"]] == [[1, 2]]


def test_disclosure_scoped_independence_corroborates_that_pair():
    (row,) = reconciliation(v3(*AGREE, assessments=[assessment(1, 1, 2, "INDEPENDENT_ORIGIN")])).values()

    assert (row["reconciliation_status"], row["corroborated"]) == ("CONSISTENT_SOURCES", True)
    (pair,) = json.loads(row["independence"])["qualifying_pairs"]
    assert pair["source_ids"] == [1, 2]
    assert [c["assessment_id"] for c in pair["independence"]] == [1]


def test_pair_wide_independence_is_not_applied_to_claims():
    (row,) = reconciliation(v3(*AGREE, assessments=[
        assessment(1, 1, 2, "INDEPENDENT_ORIGIN", scoped=False)])).values()

    assert row["reconciliation_status"] == "INDEPENDENCE_UNVERIFIED"


def test_recorded_dependence_is_related():
    (row,) = reconciliation(v3(*AGREE, assessments=[
        assessment(1, 1, 2, "SAME_CORPORATE_GROUP", scoped=False)])).values()

    assert (row["reconciliation_status"], row["corroborated"]) == ("CONSISTENT_RELATED_SOURCES", False)


def test_conflicting_assessments_block_and_stay_visible():
    (row,) = reconciliation(v3(*AGREE, assessments=[
        assessment(1, 1, 2, "INDEPENDENT_ORIGIN"),
        assessment(2, 1, 2, "QUOTES_OR_ATTRIBUTES", scoped=False)])).values()

    assert (row["reconciliation_status"], row["corroborated"]) == (
        "CONFLICTING_INDEPENDENCE_ASSESSMENTS", False)
    (conflict,) = json.loads(row["independence"])["conflicts"]
    ids = {c["assessment_id"] for c in conflict["independence"] + conflict["dependence"]}
    assert ids == {1, 2}
    assert all(c["rationale"].startswith("Synthetic rationale")
               for c in conflict["independence"] + conflict["dependence"])


def test_shared_publisher_overrides_recorded_independence_as_conflict():
    (row,) = reconciliation(v3(*AGREE, assessments=[assessment(1, 1, 2, "INDEPENDENT_ORIGIN")],
                               publishers=("Same plc", "SAME PLC.", "C sa"))).values()

    assert row["reconciliation_status"] == "CONFLICTING_INDEPENDENCE_ASSESSMENTS"
    assert json.loads(row["independence"])["conflicts"][0]["automatic"] == "SHARED_PUBLISHER"


def test_mixed_group_names_only_the_qualifying_pair():
    snapshot = v3(claim(1, 1), claim(2, 2), claim(3, 3), assessments=[
        assessment(1, 1, 2, "INDEPENDENT_ORIGIN"),
        assessment(2, 1, 3, "REPUBLICATION", scoped=False),
    ])
    (row,) = reconciliation(snapshot).values()
    details = json.loads(row["independence"])

    assert row["reconciliation_status"] == "CONSISTENT_SOURCES"
    assert [p["source_ids"] for p in details["qualifying_pairs"]] == [[1, 2]]
    assert [p["source_ids"] for p in details["dependent_pairs"]] == [[1, 3]]
    assert [p["source_ids"] for p in details["unverified_pairs"]] == [[2, 3]]


def test_scope_respects_metric_and_period():
    snapshot = v3(claim(1, 1), claim(2, 2),
                  claim(3, 1, metric="revenue"), claim(4, 2, metric="revenue"),
                  claim(5, 1, period=datetime.date(2025, 9, 30)),
                  claim(6, 2, period=datetime.date(2025, 9, 30)),
                  assessments=[assessment(1, 1, 2, "INDEPENDENT_ORIGIN"),
                               assessment(2, 1, 2, "QUOTES_OR_ATTRIBUTES", metric="revenue")])
    result = reconciliation(snapshot)

    assert result[("interest_pct", "2025-12-31")]["reconciliation_status"] == "CONSISTENT_SOURCES"
    assert result[("revenue", "2025-12-31")]["reconciliation_status"] == "CONSISTENT_RELATED_SOURCES"
    assert result[("interest_pct", "2025-09-30")]["reconciliation_status"] == "INDEPENDENCE_UNVERIFIED"


def test_disclosure_scope_survives_corrected_claim_ids():
    # A correction replaces claim 2 with claim 99; the disclosure is the same.
    (row,) = reconciliation(v3(claim(1, 1), claim(99, 2), assessments=[
        assessment(1, 1, 2, "INDEPENDENT_ORIGIN")])).values()

    assert row["reconciliation_status"] == "CONSISTENT_SOURCES"


def test_superseded_independence_no_longer_counts():
    (row,) = reconciliation(v3(*AGREE, assessments=[
        assessment(1, 1, 2, "INDEPENDENT_ORIGIN"),
        assessment(2, 1, 2, "UNKNOWN", supersedes=1)])).values()

    assert row["reconciliation_status"] == "INDEPENDENCE_UNVERIFIED"


# Versions

def test_assessments_never_change_version_1_or_2():
    plain = v3(*AGREE)
    assessed = v3(*AGREE, assessments=[assessment(1, 1, 2, "INDEPENDENT_ORIGIN")])

    for version in ("1", "2"):
        assert snapshot_sha256(as_version(plain, version)) == snapshot_sha256(as_version(assessed, version))
        assert "source_assessments" not in as_version(assessed, version)
    assert snapshot_sha256(plain) != snapshot_sha256(assessed)
    # v2 reconciliation of the same evidence ignores the assessment.
    (row,) = reconciliation(as_version(assessed, "2")).values()
    assert row["reconciliation_status"] == "INDEPENDENCE_UNVERIFIED" and "independence" not in row


def test_not_installed_differs_from_no_assessments():
    installed = v3(*AGREE)
    absent = v3(*AGREE, schema="NOT_INSTALLED")

    assert installed["source_assessments_schema"] == "INSTALLED"
    assert absent["source_assessments_schema"] == "NOT_INSTALLED"
    assert snapshot_sha256(installed) != snapshot_sha256(absent)
    assert snapshot_sha256(as_version(installed, "2")) == snapshot_sha256(as_version(absent, "2"))


def test_assessment_outside_the_snapshot_sources_is_rejected():
    with pytest.raises(ValueError, match="outside the snapshot"):
        v3(*AGREE, assessments=[assessment(1, 1, 9, "UNKNOWN")])


def test_version_3_cannot_be_derived_from_version_2():
    with pytest.raises(ValueError, match="Cannot express"):
        as_version(as_version(v3(*AGREE), "2"), "3")


# Reports, registry and currency

def run(snapshot, root, run_id):
    result = generate_versioned_report(case_id=7, output_root=root, snapshot=snapshot,
                                       run_id=run_id, now=NOW, code_version=CODE)
    data = prepare_run(7, run_id, root)
    return result["run_dir"], {"run": data["run"], "claims": data["claims"], "sources": data["sources"]}


def test_version_3_report_explains_assessments(tmp_path):
    snapshot = v3(*AGREE, assessments=[
        assessment(1, 1, 2, "INDEPENDENT_ORIGIN"),
        assessment(2, 1, 2, "UNKNOWN", scoped=False),
    ])
    snapshot["source_assessments"][0]["rationale"] = "Synthetic | piped\nrationale"
    run_dir, stored = run(snapshot, tmp_path, "run-v3")
    report = (run_dir / "report.md").read_text("utf-8")

    assert stored["run"]["snapshot_schema_version"] == "3"
    assert "#### Source independence" in report
    # Untrusted text is escaped inside the table.
    assert "Synthetic \\| piped rationale" in report
    assert "S1 and S2 — supports corroboration (assessments #1)" in report
    assert "they are not proof of independence" in report
    assert "**CONFLICTING_INDEPENDENCE_ASSESSMENTS**" in report
    assert "synthetic reviewer, 2026-02-01T12:00:00.123456Z" in report


@pytest.mark.parametrize("schema, assessments, expected", [
    ("NOT_INSTALLED", [], "does not record source-relationship assessments"),
    ("INSTALLED", [], "No source-relationship assessments are recorded"),
])
def test_version_3_report_distinguishes_absent_and_empty(tmp_path, schema, assessments, expected):
    run_dir, _ = run(v3(*AGREE, assessments=assessments, schema=schema), tmp_path, "run-v3")

    assert expected in (run_dir / "report.md").read_text("utf-8")


def test_conflict_listed_with_assessment_ids(tmp_path):
    run_dir, _ = run(v3(*AGREE, assessments=[
        assessment(1, 1, 2, "INDEPENDENT_ORIGIN"),
        assessment(2, 1, 2, "SAME_PRIMARY_DISCLOSURE", scoped=False)]), tmp_path, "run-v3")
    report = (run_dir / "report.md").read_text("utf-8")

    assert "S1 and S2 — conflicting assessments (assessments #1, #2)" in report
    assert "| CONFLICTING_INDEPENDENCE_ASSESSMENTS | S1, S2 |" in report


@pytest.mark.parametrize("damage, message", [
    (lambda m: m["evidence_snapshot"].update(source_assessments_schema="MAYBE"),
     "Invalid source-assessment schema state"),
    (lambda m: m["evidence_snapshot"]["source_assessments"][0].update(relationship="LIKELY"),
     "Invalid assessment relationship"),
    (lambda m: m["evidence_snapshot"]["source_assessments"][0].update(source_b_id=9),
     "outside the snapshot"),
    (lambda m: m["evidence_snapshot"]["source_assessments"][0].update(supersedes_assessment_id=5),
     "supersedes an unknown"),
    (lambda m: m["analysis"]["reconciliation"][0].pop("independence"),
     "Missing independence details"),
])
def test_version_3_manifest_validation(tmp_path, damage, message):
    run_dir, _ = run(v3(*AGREE, assessments=[assessment(1, 1, 2, "UNKNOWN")]), tmp_path, "run-v3")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    validate_manifest(copy.deepcopy(manifest), 7, "run-v3")

    damage(manifest)
    with pytest.raises(ManifestError, match=message):
        validate_manifest(manifest, 7, "run-v3")


def test_currency_by_version_after_a_new_assessment(tmp_path):
    before = v3(*AGREE)
    after = v3(*AGREE, assessments=[assessment(1, 1, 2, "INDEPENDENT_ORIGIN")])
    _, run3 = run(before, tmp_path, "run-v3")
    _, run2 = run(as_version(before, "2"), tmp_path, "run-v2")
    _, run1 = run(as_version(before, "1"), tmp_path, "run-v1")

    assert check_currency(run3, lambda c, y: after)["status"] == "STALE"
    assert check_currency(run2, lambda c, y: after)["status"] == "CURRENT"
    assert check_currency(run1, lambda c, y: after)["status"] == "CURRENT"
    assert check_currency(run3, lambda c, y: before)["status"] == "CURRENT"
    # Present evidence without assessments cannot be compared as version 3.
    assert check_currency(run3, lambda c, y: as_version(before, "2"))["status"] == "UNKNOWN"
