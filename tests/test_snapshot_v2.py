
"""
Evidence snapshot version 2: exact version-1 compatibility,
correction provenance, lineage diagnostics, reports and currency.
"""

import copy
import datetime

import pytest

import src.evidence_snapshot as snap
from src.claim_corrections import diagnose_lineages
from src.evidence_snapshot import (
    as_version,
    load_evidence_snapshot,
    snapshot_sha256,
)

from test_evidence_snapshot import UTC_SETTINGS, connection_with, db_rows

T = datetime.datetime(2026, 2, 1, 12, 0, 0, 123456)

# Hashes of the fixed rows below. The version-1 value was produced by
# the pre-22.4B code (commit 3d39f90); it must never change.
V1_HASH = "17b529873e3ba861f5fe591df92f31d5878bcc23443f9de0bde9b34b0c72b03e"
V2_HASH = "2ec5397fa30b680d826cf57d7a5ba7e51b686cecbb5b241f6f9895dd27c8d8fa"


def claim_row(claim_id, value, normalized, location):
    return {
        "claim_id": claim_id, "claim_fingerprint": f"{claim_id:064x}",
        "source_id": 3, "entity_name": "Northwind Holdings plc",
        "metric": "equity_interest_pct", "report_year": 2025,
        "period_end": datetime.date(2025, 9, 30), "value_text": value,
        "normalized_value": normalized, "source_location": location,
        "created_at": T,
    }


def correction_row(correction_id, original, replacement, before, after,
                   kind="CORRECTION", restored=None, evidence=3, location=None):
    return {
        "correction_id": correction_id, "original_claim_id": original,
        "replacement_claim_id": replacement, "correction_reason": "Fictional",
        "corrected_at_utc": T, "original_source_id": 3,
        "original_metric": "equity_interest_pct", "original_report_year": 2025,
        "original_period_end": datetime.date(2025, 9, 30),
        "original_value": before[0], "original_location": before[1],
        "original_fingerprint": f"{original:064x}",
        "replacement_value": after[0], "replacement_location": after[1],
        "replacement_fingerprint": f"{replacement:064x}",
        "correction_kind": kind, "restored_claim_id": restored,
        "evidence_source_id": evidence,
        "evidence_location": location if evidence is not None else None,
    }


def a_b_a_rows(lineage_schema="INSTALLED"):
    """A (41) -> B (42) -> A again (43), with recorded evidence."""

    return {
        "captured": [{"captured_at_utc": T}],
        "sources": [{
            "source_id": 3, "document_title": "Fictional Report",
            "publisher": None, "source_url": "https://example.org/r.pdf",
            "publication_date": datetime.date(2025, 4, 1),
            "evidence_assessment": "SUPPORTS", "added_at": T,
        }],
        "history": [{
            "history_id": 1, "source_id": 3, "event_type": "REVIEW",
            "previous_assessment": None, "new_assessment": "SUPPORTS",
            "review_reason": "Fictional review", "recorded_at_utc": T,
        }],
        "annotations": [], "metadata_changes": [],
        "claims": [claim_row(43, "49%", "49", "Page 4")],
        "corrections": [
            correction_row(1, 41, 42, ("49%", "Page 4"), ("51%", "Page 5"),
                           location="Page 5"),
            correction_row(2, 42, 43, ("51%", "Page 5"), ("49%", "Page 4"),
                           kind="RESTORATION", restored=41, location="Page 4"),
        ],
        "historical_claims": [
            claim_row(41, "49%", "49", "Page 4"),
            claim_row(42, "51%", "51", "Page 5"),
        ],
        "lineage_schema": lineage_schema,
    }


def assemble(rows):
    """A version-2 snapshot: today's assembly projected to version 2."""
    rows = {"source_assessments": [], "source_assessments_schema": "NOT_INSTALLED", **rows}
    return as_version(snap._assemble(7, (2024, 2025), rows), "2")


# Exact version-1 compatibility

def test_version_1_hash_is_fixed():
    v1 = as_version(assemble(a_b_a_rows()), "1")

    assert snapshot_sha256(v1) == V1_HASH
    assert v1["snapshot_schema_version"] == "1"
    assert set(v1) == {"snapshot_schema_version", "case_id", "years",
                       "captured_at_utc", "claims", "sources", "corrections"}
    assert all(set(c) == {
        "correction_id", "original_claim_id", "replacement_claim_id",
        "correction_reason", "corrected_at_utc", "source_id", "metric",
        "report_year", "period_end", "original_value", "original_location",
        "original_fingerprint", "replacement_value", "replacement_location",
        "replacement_fingerprint",
    } for c in v1["corrections"])


def test_version_2_hash_is_fixed_and_differs():
    v2 = assemble(a_b_a_rows())

    assert snapshot_sha256(v2) == V2_HASH
    assert V2_HASH != V1_HASH


def test_version_1_ignores_lineage_metadata():
    # The same evidence with or without lineage metadata is the
    # same version-1 evidence.
    legacy = a_b_a_rows("NOT_INSTALLED")
    for row in legacy["corrections"]:
        row.update(correction_kind="CORRECTION", restored_claim_id=None,
                   evidence_source_id=None, evidence_location=None)

    assert snapshot_sha256(as_version(assemble(legacy), "1")) == V1_HASH


def test_projection_rules():
    v2 = assemble(a_b_a_rows())
    v1 = as_version(v2, "1")

    assert as_version(v2, "2") is v2
    assert as_version(v1, "1") is v1
    with pytest.raises(ValueError, match="Cannot express"):
        as_version(v1, "2")
    with pytest.raises(ValueError, match="Cannot express"):
        as_version(v2, "3")
    with pytest.raises(ValueError, match="Unsupported"):
        as_version(v2, "9")
    # Projection does not alter the original.
    assert "historical_claims" in v2 and v2["corrections"][0]["correction_kind"]


def test_unsupported_version_rejected_before_reading():
    with pytest.raises(ValueError, match="Unsupported snapshot version"):
        load_evidence_snapshot(case_id=7, connection=object(), version="9")


# Version-2 content

def test_version_2_records_complete_provenance():
    v2 = assemble(a_b_a_rows())

    assert [c["claim_id"] for c in v2["claims"]] == [43]
    assert [c["claim_id"] for c in v2["historical_claims"]] == [41, 42]
    restoration = v2["corrections"][1]
    assert restoration["correction_kind"] == "RESTORATION"
    assert restoration["restored_claim_id"] == 41
    assert (restoration["evidence_source_id"], restoration["evidence_location"]) == (3, "Page 4")
    assert restoration["attribution_recorded"] is True
    assert v2["lineage_schema"] == "INSTALLED"


def test_legacy_schema_reads_without_inventing_attribution():
    rows = db_rows(corrections=[
        correction_row(1, 40, 41, ("48%", "Page 3"), ("49%", "Page 4"), evidence=None)
        | {"correction_kind": "CORRECTION"},
    ])
    rows.append([claim_row(40, "48%", "48", "Page 3")])  # historical claims
    connection, cursor = connection_with(rows, settings=UTC_SETTINGS)

    v2 = load_evidence_snapshot(case_id=7, connection=connection)

    (correction,) = v2["corrections"]
    assert v2["lineage_schema"] == "NOT_INSTALLED"
    assert correction["correction_kind"] == "CORRECTION"
    assert correction["restored_claim_id"] is None
    assert correction["evidence_source_id"] is None
    assert correction["attribution_recorded"] is False
    assert [c["claim_id"] for c in v2["historical_claims"]] == [40]
    # The legacy query selects literals, not the missing columns.
    queries = " ".join(c.args[0] for c in cursor.execute.call_args_list)
    assert "NULL AS evidence_source_id" in queries
    # Historical claims come only from this investigation.
    assert "s.case_id = %s" in cursor.execute.call_args_list[-1].args[0]


def test_partial_lineage_schema_is_an_error():
    rows = db_rows(lineage_columns=["correction_kind", "restored_claim_id"])
    connection, _ = connection_with(rows, settings=UTC_SETTINGS)

    with pytest.raises(ValueError, match="only part of the correction-lineage schema"):
        load_evidence_snapshot(case_id=7, connection=connection)


def test_installed_schema_selects_lineage_columns():
    rows = db_rows(lineage_columns=snap.LINEAGE_COLUMNS)
    connection, cursor = connection_with(rows, settings=UTC_SETTINGS)

    v2 = load_evidence_snapshot(case_id=7, connection=connection)

    assert v2["lineage_schema"] == "INSTALLED"
    assert v2["corrections"] == [] and v2["historical_claims"] == []
    queries = " ".join(c.args[0] for c in cursor.execute.call_args_list)
    assert "cc.correction_kind, cc.restored_claim_id" in queries


# Lineage diagnostics

def versions(*claims):
    return [{"claim_id": i, "value_text": v, "source_location": "Page 4"} for i, v in claims]


def corrections(*rows):
    return [
        {"correction_id": cid, "original_claim_id": o, "replacement_claim_id": r,
         "correction_kind": kind, "restored_claim_id": restored,
         "evidence_source_id": evidence}
        for cid, o, r, kind, restored, evidence in rows
    ]


def test_diagnostics_statuses():
    assert diagnose_lineages([], [])["status"] == "NO_CORRECTIONS"

    complete = diagnose_lineages(
        versions((1, "49"), (2, "51"), (3, "49")),
        corrections((10, 1, 2, "CORRECTION", None, 5), (11, 2, 3, "RESTORATION", 1, 5)),
        source_ids={5},
    )
    assert (complete["status"], complete["lineages"], complete["problems"]) == (
        "NO_PROBLEMS_FOUND", 1, [])


def test_missing_historical_claim_is_incomplete_not_clean():
    result = diagnose_lineages(
        versions((2, "51"), (3, "52")),
        corrections((10, 1, 2, "CORRECTION", None, 5), (11, 2, 3, "CORRECTION", None, 5)),
    )

    assert result["status"] == "INCOMPLETE"
    assert result["missing_claims"] == [1]
    assert any("claim 1 is referenced" in p for p in result["problems"])


@pytest.mark.parametrize("rows, problem", [
    (((10, 1, 2, "CORRECTION", None, 5), (11, 1, 3, "CORRECTION", None, 5)),
     "more than one replacement"),
    (((10, 2, 1, "CORRECTION", None, 5),), "runs backwards"),
    (((10, 1, 2, "CORRECTION", None, 5), (11, 2, 3, "RESTORATION", 3, 5)),
     "outside the earlier lineage"),
    (((10, 1, 2, "CORRECTION", None, 99),), "outside this investigation"),
])
def test_confirmed_problems_take_precedence(rows, problem):
    result = diagnose_lineages(versions((1, "49"), (2, "51"), (3, "49")),
                               corrections(*rows), source_ids={5})

    assert result["status"] == "PROBLEMS_FOUND"
    assert any(problem in p for p in result["problems"]), result["problems"]


def test_missing_attribution_counted():
    result = diagnose_lineages(versions((1, "49"), (2, "51")),
                               corrections((10, 1, 2, "CORRECTION", None, None)))

    assert result["status"] == "NO_PROBLEMS_FOUND"
    assert result["attribution_not_recorded"] == 1


# Reports, registry and currency

import json  # noqa: E402

from src.generate_versioned_report import generate_versioned_report  # noqa: E402
from src.report_registry import ManifestError, prepare_run, validate_manifest  # noqa: E402
from src.report_verification import check_currency  # noqa: E402

NOW = datetime.datetime(2026, 2, 1, 12, 0, tzinfo=datetime.timezone.utc)
CODE = {"git_commit": "a" * 40, "working_tree_dirty": False}


def run(snapshot, root, run_id="run-v2"):
    result = generate_versioned_report(
        case_id=7, output_root=root, snapshot=snapshot, run_id=run_id,
        now=NOW, code_version=CODE,
    )
    rows = prepare_run(7, run_id, root)
    stored = {"run": rows["run"], "claims": rows["claims"], "sources": rows["sources"]}
    return result["run_dir"], stored


def test_version_2_report_shows_provenance_and_diagnostics(tmp_path):
    run_dir, stored = run(assemble(a_b_a_rows()), tmp_path)
    report = (run_dir / "report.md").read_text()

    assert stored["run"]["snapshot_schema_version"] == "2"
    assert "| Correction | Event | Superseded claim | Replacement claim " \
           "| Restored version | Reason | Supporting evidence | Corrected (UTC) |" in report
    assert "| #2 | Restoration | C42: " in report and "| C41 |" in report
    assert "S3 (Fictional Report): Page 4" in report
    assert "#### Lineage diagnostics" in report
    assert "**Status: no problems found** in 1 lineage(s) covering 2 correction(s)." in report


def test_version_2_report_states_unrecorded_attribution(tmp_path):
    rows = a_b_a_rows("NOT_INSTALLED")
    rows["corrections"] = rows["corrections"][:1]
    rows["corrections"][0].update(evidence_source_id=None, evidence_location=None)
    rows["historical_claims"] = rows["historical_claims"][:1]
    rows["claims"] = [claim_row(42, "51%", "51", "Page 5")]
    run_dir, _ = run(assemble(rows), tmp_path)
    report = (run_dir / "report.md").read_text()

    assert "| Not recorded |" in report
    assert "does not record correction-lineage metadata" in report


def test_version_2_report_marks_incomplete_lineage(tmp_path):
    rows = a_b_a_rows()
    rows["historical_claims"] = rows["historical_claims"][1:]  # claim 41 unavailable
    run_dir, _ = run(assemble(rows), tmp_path)
    report = (run_dir / "report.md").read_text()

    assert "**Status: INCOMPLETE.**" in report
    assert "C41 (not available)" in report
    assert "no problems found" not in report


def test_untrusted_text_is_escaped(tmp_path):
    rows = a_b_a_rows()
    rows["corrections"][0]["correction_reason"] = "Misread | page\nnext line"
    run_dir, _ = run(assemble(rows), tmp_path)
    report = (run_dir / "report.md").read_text()

    assert "Misread \\| page next line" in report


def test_version_1_report_is_unchanged_by_version_2(tmp_path, snapshot_tools):
    t = snapshot_tools
    v1 = t.snapshot([t.claim(11, 1, "49", year=2024), t.claim(12, 2, "49", year=2025)])
    run_dir, stored = run(v1, tmp_path, run_id="run-v1")
    manifest = json.loads((run_dir / "manifest.json").read_text())

    assert stored["run"]["snapshot_schema_version"] == "1"
    assert "lineage" not in manifest["analysis"]
    assert "#### Lineage diagnostics" not in (run_dir / "report.md").read_text()


@pytest.mark.parametrize("damage, message", [
    (lambda m: m["evidence_snapshot"].update(snapshot_schema_version="9"),
     "Unsupported snapshot schema version"),
    (lambda m: m["evidence_snapshot"]["corrections"][0].update(attribution_recorded=False),
     "Inconsistent evidence attribution"),
    (lambda m: m["evidence_snapshot"]["corrections"][1].update(correction_kind="REWRITE"),
     "Invalid correction kind"),
    (lambda m: m["evidence_snapshot"].pop("historical_claims"), "Missing historical claims"),
    (lambda m: m["evidence_snapshot"]["historical_claims"].append(
        m["evidence_snapshot"]["claims"][0]), "both current and historical"),
    (lambda m: m["analysis"].pop("lineage"), "Missing lineage diagnostics"),
])
def test_version_2_manifest_validation(tmp_path, damage, message):
    run_dir, _ = run(assemble(a_b_a_rows()), tmp_path)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    validate_manifest(copy.deepcopy(manifest), 7, "run-v2")

    damage(manifest)
    with pytest.raises(ManifestError, match=message):
        validate_manifest(manifest, 7, "run-v2")


def test_currency_for_version_2_runs(tmp_path):
    v2 = assemble(a_b_a_rows())
    _, stored = run(v2, tmp_path)

    assert check_currency(stored, lambda c, y: v2)["status"] == "CURRENT"

    changed = copy.deepcopy(v2)
    changed["corrections"][1]["evidence_location"] = "Page 9"
    assert check_currency(stored, lambda c, y: changed)["status"] == "STALE"

    # Present evidence that lacks version-2 provenance is not comparable.
    result = check_currency(stored, lambda c, y: as_version(v2, "1"))
    assert result["status"] == "UNKNOWN"


def test_currency_for_version_1_runs_uses_version_1(tmp_path):
    v2 = assemble(a_b_a_rows())
    _, stored = run(as_version(v2, "1"), tmp_path, run_id="run-v1")

    # Today's version-2 evidence, compared as version 1.
    assert check_currency(stored, lambda c, y: v2)["status"] == "CURRENT"

    # A provenance-only change is invisible to version 1 ...
    attributed = copy.deepcopy(v2)
    attributed["corrections"][0]["evidence_location"] = "Note 9"
    assert check_currency(stored, lambda c, y: attributed)["status"] == "CURRENT"

    # ... but a claim change makes it STALE.
    changed = copy.deepcopy(v2)
    changed["claims"][0]["source_location"] = "Page 10"
    assert check_currency(stored, lambda c, y: changed)["status"] == "STALE"


def test_unknown_registered_version_is_unknown(tmp_path):
    v2 = assemble(a_b_a_rows())
    _, stored = run(v2, tmp_path)
    stored["run"]["snapshot_schema_version"] = "9"

    result = check_currency(stored, lambda c, y: v2)

    assert result["status"] == "UNKNOWN"
    assert "not supported" in result["problems"][0]


# Final review: invariants, rendering, currency semantics

from pathlib import Path  # noqa: E402

from src.generate_versioned_report import render_run  # noqa: E402
from src.report_renderer import analyze_snapshot, render_report  # noqa: E402

V1_REPORT_SHA256 = "3ae999eb7f13ba9699b1482390d80259add82c45b4fc14d8b3f3e803604d9925"
SPP_MANIFESTS = sorted(Path(__file__).resolve().parents[1].glob(
    "reports/runs/case_1/*/manifest.json"))


def test_version_1_report_bytes_are_fixed():
    # Produced by the pre-22.4B renderer (commit 3d39f90) from the
    # same snapshot, which includes an A -> B -> A correction history.
    import hashlib

    v1 = as_version(assemble(a_b_a_rows()), "1")
    metadata = {
        "run_id": "run-fixed", "generated_at_utc": "2026-02-01T12:00:00.000000Z",
        "git_commit": "a" * 40, "working_tree_dirty": False,
        "evidence_snapshot_sha256": "0" * 64, "previous_year": 2024,
        "current_year": 2025,
    }
    report = render_report(v1, analyze_snapshot(v1, 2024, 2025), metadata)

    assert hashlib.sha256(report.encode("utf-8")).hexdigest() == V1_REPORT_SHA256


@pytest.mark.skipif(not SPP_MANIFESTS, reason="no local SPP report run")
def test_historical_spp_report_re_renders_byte_identically():
    manifest = json.loads(SPP_MANIFESTS[0].read_text("utf-8"))
    parameters = manifest["parameters"]

    report, _, _ = render_run(
        manifest["evidence_snapshot"], parameters["previous_year"],
        parameters["current_year"], manifest["run_id"],
        manifest["generated_at_utc"], manifest["generator"],
    )

    assert manifest["evidence_snapshot"]["snapshot_schema_version"] == "1"
    assert snapshot_sha256(manifest["evidence_snapshot"]) == (
        "ae0ab1379c2cf5536d7b506a772335bc198b4630c172b7034d9de8bf0b3148c7")
    import hashlib
    assert hashlib.sha256(report).hexdigest() == manifest["report"]["sha256"]


def identity(claim_id, value, **overrides):
    return {"claim_id": claim_id, "value_text": value, "source_location": "Page 4",
            "source_id": 3, "entity": "Fictional plc", "metric": "interest_pct",
            "report_year": 2025, "period_end": "2025-12-31", **overrides}


@pytest.mark.parametrize("field, value, label", [
    ("metric", "other_metric", "metric"),
    ("report_year", 2026, "report year"),
    ("period_end", "2026-06-30", "period end"),
    ("source_id", 4, "source"),
])
def test_correction_changing_claim_identity_is_a_problem(field, value, label):
    result = diagnose_lineages(
        [identity(1, "49"), identity(2, "51", **{field: value})],
        corrections((10, 1, 2, "CORRECTION", None, 3)), current_ids={2},
    )

    assert result["status"] == "PROBLEMS_FOUND"
    assert any(f"changes the claim's {label}" in p for p in result["problems"])


def test_evidence_from_another_source_does_not_fragment_lineage():
    # Each step cites a different supporting source; the claim's own
    # source never changes, so the chain stays whole.
    result = diagnose_lineages(
        [identity(1, "49"), identity(2, "51"), identity(3, "49")],
        corrections((10, 1, 2, "CORRECTION", None, 5),
                    (11, 2, 3, "RESTORATION", 1, 6)),
        source_ids={3, 5, 6}, current_ids={3},
    )

    assert (result["status"], result["lineages"]) == ("NO_PROBLEMS_FOUND", 1)


def test_lineage_ending_at_superseded_claim_is_incomplete():
    # The correction that superseded claim 2 is not in the data.
    result = diagnose_lineages(
        [identity(1, "49"), identity(2, "51")],
        corrections((10, 1, 2, "CORRECTION", None, 3)), current_ids={9},
    )

    assert result["status"] == "INCOMPLETE"
    assert any("ends at claim 2, which is not current" in p for p in result["problems"])


def changed(snapshot, edit):
    copy_ = copy.deepcopy(snapshot)
    edit(copy_)
    return copy_


def new_claim(s):
    s["claims"].append(dict(s["claims"][0], claim_id=99, claim_fingerprint="9" * 64))


def corrected_claim(s):
    s["historical_claims"].append(s["claims"][0])
    s["claims"][0] = dict(s["claims"][0], claim_id=44, claim_fingerprint="4" * 64,
                          value="52%", normalized_value="52")
    s["corrections"].append(dict(s["corrections"][1], correction_id=3,
                                 original_claim_id=43, replacement_claim_id=44,
                                 correction_kind="CORRECTION", restored_claim_id=None))


def restored_value(s):
    corrected_claim(s)
    s["historical_claims"].append(s["claims"][0])
    s["claims"][0] = dict(s["claims"][0], claim_id=45, claim_fingerprint="5" * 64,
                          value="49%", normalized_value="49")
    s["corrections"].append(dict(s["corrections"][1], correction_id=4,
                                 original_claim_id=44, replacement_claim_id=45,
                                 correction_kind="RESTORATION", restored_claim_id=43))


def provenance_only(s):
    s["corrections"][0]["evidence_location"] = "Note 12"


def lineage_columns_installed(s):
    s["lineage_schema"] = "INSTALLED"


@pytest.mark.parametrize("change, v1_status, v2_status", [
    (new_claim, "STALE", "STALE"),
    (corrected_claim, "STALE", "STALE"),
    (restored_value, "STALE", "STALE"),
    (provenance_only, "CURRENT", "STALE"),
    (lineage_columns_installed, "CURRENT", "STALE"),
])
def test_currency_semantics(tmp_path, change, v1_status, v2_status):
    rows = a_b_a_rows("NOT_INSTALLED" if change is lineage_columns_installed else "INSTALLED")
    baseline = assemble(rows)
    present = changed(baseline, change)
    _, v1_run = run(as_version(baseline, "1"), tmp_path, run_id="run-v1")
    _, v2_run = run(baseline, tmp_path, run_id="run-v2")

    v1_result = check_currency(v1_run, lambda c, y: present)
    v2_result = check_currency(v2_run, lambda c, y: present)

    assert (v1_result["status"], v2_result["status"]) == (v1_status, v2_status)
    # STALE means the evidence differs, never that the report is damaged.
    for result in (v1_result, v2_result):
        assert result["status"] in ("CURRENT", "STALE")


@pytest.mark.parametrize("failure", [
    ConnectionError("database unavailable"),
    ValueError("claim_corrections has only part of the correction-lineage schema"),
])
def test_unavailable_or_inconsistent_evidence_is_unknown(tmp_path, failure):
    _, stored = run(assemble(a_b_a_rows()), tmp_path)

    def load(case_id, years):
        raise failure

    result = check_currency(stored, load)

    assert result["status"] == "UNKNOWN"
    assert type(failure).__name__ in result["problems"][0]
