
"""Snapshot version 4 (Milestone 23B.2C): identity and GLEIF context, kept apart. No database."""

import copy
import json

import pytest

import src.evidence_snapshot as snap
from src.evidence_snapshot import as_version, canonical_json, snapshot_sha256
from src.generate_versioned_report import generate_versioned_report, verify_run
from src.gleif_observations import NOT_CAPTURED
from src.report_registry import ManifestError, prepare_run, validate_manifest
from src.report_renderer import analyze_snapshot, render_report
from src.relationship_history import (ENTITY_FIELDS, RELATIONSHIP_FIELDS, assessment, compare)

from test_snapshot_v3 import AGREE, CODE, NOW, T, assessment as source_assessment, rows

LEI_A, LEI_B = "SYNTHA00000000000047", "SYNTHB00000000000016"


def mapping(i, subject, status, lei=None, candidates=None, supersedes=None):
    return {"mapping_id": i, "case_id": 7, "subject_text": subject, "status": status, "lei": lei,
            "name_type": "LEGAL_NAME" if lei else None, "candidate_leis": candidates,
            "basis": "DOCUMENTED", "evidence_source_id": None, "evidence_reference": "Register",
            "evidence_location": "Entry 1", "rationale": f"Synthetic rationale {i}",
            "limitations": "Synthetic", "recorded_by": "synthetic reviewer",
            "recorded_at_utc": T, "supersedes_mapping_id": supersedes}


def relationship_history():
    def entry(origin, when, status):
        values = {name: None for name in RELATIONSHIP_FIELDS}
        values.update(relationship_status=status, validation_sources="ENTITY_SUPPLIED_ONLY")
        return {"origin": origin, "source": f"file {when}", "source_file_id": None if origin ==
                "LEGACY_IMPORT" else 1, "data_valid_as_of_utc": when, "values": values}
    entries = [entry("LEGACY_IMPORT", "2025-10-22T09:00:01.000", NOT_CAPTURED),
               entry("GLEIF_OBSERVATION", "2026-01-01T00:00:00.000", "ACTIVE"),
               entry("GLEIF_OBSERVATION", "2026-02-01T00:00:00.000", "INACTIVE")]
    comparison = compare(entries, RELATIONSHIP_FIELDS)
    return {"subject": {"child_lei": LEI_A, "parent_lei": LEI_B,
                        "relationship_type": "IS_DIRECTLY_CONSOLIDATED_BY"},
            "interpretation": "An accounting-consolidation relationship is reported for financial "
                              "reporting. It is not evidence of direct ownership or managerial control.",
            "entries": entries, "comparison": comparison,
            "assessment": assessment(comparison, entries),
            "absences": [], "audit": {"observations_only_in_incomplete_batches": 1,
                                      "quarantined_records": 2}}


def entity_history(lei):
    values = {f: None for f in ENTITY_FIELDS}
    values["legal_name"] = {"text": "Synthetic Holdings AG", "lang": "de"}
    entries = [{"origin": "GLEIF_OBSERVATION", "source": "file jan", "source_file_id": 1,
                "data_valid_as_of_utc": "2026-01-01T00:00:00.000", "values": values}]
    comparison = compare(entries, ENTITY_FIELDS)
    return {"subject": {"lei": lei}, "entries": entries, "comparison": comparison,
            "assessment": assessment(comparison, entries), "stated_events": [],
            "identity_row": None, "audit": {"observations_only_in_incomplete_batches": 0,
                                            "quarantined_records": 0}}


def v4_rows(mappings=(), schema=True, relationship=True, **kwargs):
    base = rows(AGREE, [source_assessment(1, 1, 2, "INDEPENDENT_ORIGIN")], **kwargs)
    base["v4"] = {"mappings": list(mappings) if schema else None,
                  "parties": (LEI_A, LEI_B) if relationship else (),
                  "observation_schema": True,
                  "relationship": relationship_history() if relationship else None,
                  "entities": [entity_history(LEI_A), entity_history(LEI_B)]}
    return base


def v4(**kwargs):
    return snap._assemble(7, (2024, 2025), v4_rows(**kwargs))


MAPPINGS = (mapping(1, "Synthetic Holdings", "UNRESOLVED"),
            mapping(2, "Synthetic Holdings", "AMBIGUOUS", candidates=f'["{LEI_A}", "{LEI_B}"]',
                    supersedes=1))


def test_version_4_projects_exactly_to_versions_1_to_3():
    four = v4(mappings=MAPPINGS)
    three = snap._assemble(7, (2024, 2025), {k: v for k, v in v4_rows(mappings=MAPPINGS).items()
                                             if k != "v4"})
    assert four["snapshot_schema_version"] == "4" and three["snapshot_schema_version"] == "3"
    for version in ("1", "2", "3"):
        assert canonical_json(as_version(four, version)) == canonical_json(as_version(three, version))
    with pytest.raises(ValueError, match="Cannot express"):
        as_version(three, "4")


def test_identity_mappings_keep_every_state_and_resolve_nothing():
    identity = v4(mappings=MAPPINGS)["case_identity"]
    assert identity["schema"] == "INSTALLED"
    assert [(m["status"], m["lei"], m["candidate_leis"]) for m in identity["mappings"]] == [
        ("UNRESOLVED", None, None), ("AMBIGUOUS", None, [LEI_A, LEI_B])]
    assert "Nothing is resolved automatically" in identity["note"]
    assert v4(schema=False)["case_identity"] == {**identity, "schema": "NOT_INSTALLED",
                                                 "mappings": []}


def test_gleif_context_is_kept_apart_and_never_corroborates():
    four = v4(mappings=MAPPINGS)
    three = as_version(four, "3")
    context = four["gleif_context"]
    assert "not independent corroboration" in context["note"]
    assert context["relationship"]["assessment"]["corporate_change_determination"] == \
        "INSUFFICIENT_EVIDENCE"
    # Findings are those of the same evidence without any GLEIF context.
    assert analyze_snapshot(four, 2024, 2025) == analyze_snapshot(three, 2024, 2025)


def test_version_4_is_deterministic():
    assert snapshot_sha256(v4(mappings=MAPPINGS)) == snapshot_sha256(v4(mappings=MAPPINGS))
    changed = v4(mappings=MAPPINGS[:1])
    assert snapshot_sha256(changed) != snapshot_sha256(v4(mappings=MAPPINGS))


def test_mapping_evidence_must_be_a_snapshot_source():
    bad = dict(mapping(1, "Synthetic Holdings", "UNRESOLVED"), evidence_source_id=99)
    with pytest.raises(ValueError, match="outside the snapshot"):
        v4(mappings=[bad])


def test_version_4_report_adds_section_i_and_leaves_earlier_sections(tmp_path):
    four = v4(mappings=MAPPINGS)
    metadata = {"run_id": "r", "generated_at_utc": "2026-02-01T12:00:00Z", "git_commit": "a" * 40,
                "working_tree_dirty": False, "evidence_snapshot_sha256": "0" * 64,
                "previous_year": 2024, "current_year": 2025}
    report4 = render_report(four, analyze_snapshot(four, 2024, 2025), metadata)
    three = as_version(four, "3")
    report3 = render_report(three, analyze_snapshot(three, 2024, 2025), metadata)

    head, section = report4.split("### I. Case Identity and Global GLEIF Context")
    assert head.rstrip("\n") == report3.rstrip("\n")             # A–H identical to version 3
    assert "### I." not in report3
    for text in ("Case-specific claim-subject identities", "| #2 | Synthetic Holdings | AMBIGUOUS |",
                 "Superseded", "Global GLEIF context (not case evidence)",
                 "not independent corroboration", "not evidence of direct ownership or managerial control",
                 "| 2025-10-22T09:00:01.000 | file 2025-10-22T09:00:01.000 | LEGACY_IMPORT | NOT CAPTURED |",
                 "Published records differ in: relationship_status.",
                 "1 observation(s) only in incomplete imports, 2 quarantined record(s)"):
        assert text in section, text


def test_version_4_runs_register_verify_and_are_created_only_on_request(tmp_path):
    result = generate_versioned_report(case_id=7, output_root=tmp_path, snapshot=v4(mappings=MAPPINGS),
                                       run_id="run-v4", now=NOW, code_version=CODE)
    assert verify_run(result["run_dir"]) == []
    data = prepare_run(7, "run-v4", tmp_path)
    assert data["run"]["snapshot_schema_version"] == "4"
    manifest = json.loads((result["run_dir"] / "manifest.json").read_text())
    validate_manifest(copy.deepcopy(manifest), 7, "run-v4")
    manifest["evidence_snapshot"]["gleif_context"]["relationship"]["assessment"][
        "corporate_change_determination"] = "CHANGE_PROVEN"
    with pytest.raises(ManifestError, match="must not determine corporate changes"):
        validate_manifest(manifest, 7, "run-v4")
    assert snap.SNAPSHOT_SCHEMA_VERSION == "3"                   # version 4 only when asked
