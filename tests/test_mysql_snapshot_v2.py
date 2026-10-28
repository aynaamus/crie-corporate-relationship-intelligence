
"""
Snapshot version 2 against the real test database
(corporate_intelligence_test only), with synthetic records.
"""

from src.claim_corrections import correct_claim, fetch_case_diagnostics, restore_claim
from src.evidence_snapshot import as_version, snapshot_sha256
from src.report_registry import register_run

# Guarded test-database helpers (identity-checked connections,
# synthetic fixtures, flagged cleanup, production-connection guard).
from test_mysql_claim_lineage import lineage_db  # noqa: F401
from test_mysql_evidence_snapshot import (  # noqa: F401
    block_default_connections,
    connect,
    pytestmark,
)
from test_mysql_report_registry import (  # noqa: F401
    currency,
    new_run,
    registry_db,
    snapshot_now,
)


def test_version_2_snapshot_reconstructs_restoration(lineage_db):
    connection, _, _, case_id, a, b, c1, c2 = lineage_db
    b_claim = correct_claim(connection, case_id, c2, "51", "Page 4", "Fictional misread",
                            evidence_source_id=a, evidence_location="Note 2")["replacement_claim_id"]
    a_again = restore_claim(connection, case_id, b_claim, c2,
                            "Fictional confirmation")["replacement_claim_id"]

    v2 = as_version(snapshot_now(case_id), "2")

    assert v2["snapshot_schema_version"] == "2"
    assert v2["lineage_schema"] == "INSTALLED"
    assert sorted(c["claim_id"] for c in v2["claims"]) == [c1, a_again]
    assert [c["claim_id"] for c in v2["historical_claims"]] == [c2, b_claim]
    first, second = v2["corrections"]
    assert (first["correction_kind"], first["evidence_source_id"], first["evidence_location"]) == (
        "CORRECTION", a, "Note 2")
    assert (second["correction_kind"], second["restored_claim_id"]) == ("RESTORATION", c2)
    # Default evidence: the claim's own source at the restored location.
    assert (second["evidence_source_id"], second["evidence_location"]) == (b, "Page 3")

    diagnostics = fetch_case_diagnostics(connection, case_id)
    connection.rollback()
    assert (diagnostics["status"], diagnostics["lineages"], diagnostics["corrections"]) == (
        "NO_PROBLEMS_FOUND", 1, 2)


def test_version_1_from_database_matches_projection(lineage_db):
    connection, _, _, case_id, _, _, _, c2 = lineage_db
    correct_claim(connection, case_id, c2, "51", "Page 4", "Fictional fix")

    reader = connect()
    try:
        reader.start_transaction(consistent_snapshot=True,
                                 isolation_level="REPEATABLE READ", readonly=True)
        from src.evidence_snapshot import load_evidence_snapshot
        v1 = load_evidence_snapshot(case_id=case_id, years=(2024, 2025),
                                    connection=reader, version="1")
        v2 = load_evidence_snapshot(case_id=case_id, years=(2024, 2025),
                                    connection=reader, version="2")
        reader.rollback()
    finally:
        reader.close()

    # captured_at_utc records when each read ran; it is not evidence.
    assert snapshot_sha256(v1) == snapshot_sha256(as_version(v2, "1"))
    assert {k: v for k, v in v1.items() if k != "captured_at_utc"} == {
        k: v for k, v in as_version(v2, "1").items() if k != "captured_at_utc"}
    assert snapshot_sha256(v1) != snapshot_sha256(v2)
    assert "historical_claims" not in v1


def test_other_investigations_are_not_included(lineage_db):
    connection, fixtures, _, case_id, _, _, _, c2 = lineage_db
    correct_claim(connection, case_id, c2, "51", "Page 4", "Fictional fix")

    # A second synthetic case with its own correction.
    other = connect()
    try:
        cursor = other.cursor()
        cursor.execute("INSERT INTO investigation_cases (case_name) VALUES ('Other synthetic')")
        other_case = cursor.lastrowid
        cursor.execute("INSERT INTO case_sources (case_id, document_title) VALUES (%s, 'Other')",
                       (other_case,))
        other_source = cursor.lastrowid
        other.commit()
        cursor.close()
    finally:
        other.close()
    fixtures.source_ids.append(other_source)
    other_claim = fixtures.claim(other_source, 2025, "10")
    connection.commit()
    try:
        correct_claim(connection, other_case, other_claim, "11", "Page 1", "Other fix")

        v2 = snapshot_now(case_id)
        ids = {c["claim_id"] for c in v2["claims"] + v2["historical_claims"]}
        assert other_claim not in ids
        assert {c["source_id"] for c in v2["sources"]} == set(fixtures.source_ids) - {other_source}
        assert len(v2["corrections"]) == 1
    finally:
        cleanup = connect()
        try:
            cursor = cleanup.cursor()
            cursor.execute("SET @crie_claim_maintenance = 1")
            cursor.execute(
                "DELETE cc FROM claim_corrections cc JOIN disclosure_claims c "
                "ON c.claim_id = cc.original_claim_id WHERE c.source_id = %s", (other_source,))
            cursor.execute("SET @crie_claim_maintenance = 0")
            cursor.execute("DELETE FROM disclosure_claims WHERE source_id = %s", (other_source,))
            cursor.execute("DELETE FROM case_sources WHERE source_id = %s", (other_source,))
            cursor.execute("DELETE FROM investigation_cases WHERE case_id = %s", (other_case,))
            cleanup.commit()
            cursor.close()
        finally:
            cleanup.close()
        fixtures.source_ids.remove(other_source)


def test_version_1_and_2_runs_registered_and_compared(lineage_db, tmp_path):
    connection, _, run_ids, case_id, _, _, _, c2 = lineage_db

    current = snapshot_now(case_id)
    run_ids += ["synthetic-v1-run", "synthetic-v2-run"]
    v1_dir = new_run(tmp_path, case_id, "synthetic-v1-run", snapshot=as_version(current, "1"))
    v2_dir = new_run(tmp_path, case_id, "synthetic-v2-run", snapshot=current)
    files = {p: p.read_bytes() for d in (v1_dir, v2_dir) for p in d.iterdir()}
    for run_id in ("synthetic-v1-run", "synthetic-v2-run"):
        register_run(connection, case_id, run_id, tmp_path)

    for run_id in ("synthetic-v1-run", "synthetic-v2-run"):
        assert currency(case_id, run_id, tmp_path)["currency"]["status"] == "CURRENT"

    b_claim = correct_claim(connection, case_id, c2, "51", "Page 4",
                            "Fictional fix")["replacement_claim_id"]
    restore_claim(connection, case_id, b_claim, c2, "Fictional restoration")

    for run_id in ("synthetic-v1-run", "synthetic-v2-run"):
        result = currency(case_id, run_id, tmp_path)
        assert result["integrity"]["status"] == "INTACT"
        assert result["currency"]["status"] == "STALE"
        assert len(result["currency"]["changes"]["corrections_added"]) == 2

    assert {p: p.read_bytes() for p in files} == files


from src.claim_corrections import fetch_lineage  # noqa: E402


def insert_raw_correction(original, replacement, evidence_source=None):
    """Bypass the application, as a faulty import or manual SQL could."""

    raw = connect()
    try:
        cursor = raw.cursor()
        cursor.execute(
            "INSERT INTO claim_corrections (original_claim_id, replacement_claim_id, "
            "correction_reason, evidence_source_id, evidence_location) "
            "VALUES (%s, %s, 'Fictional raw link', %s, %s)",
            (original, replacement, evidence_source,
             None if evidence_source is None else "Page 1"),
        )
        raw.commit()
        cursor.close()
    finally:
        raw.close()


def test_changing_evidence_sources_keep_one_lineage(lineage_db):
    connection, _, _, case_id, a, b, _, c2 = lineage_db
    step = correct_claim(connection, case_id, c2, "50", "Page 4", "Fictional step 1",
                         evidence_source_id=a, evidence_location="Note 1")["replacement_claim_id"]
    step = correct_claim(connection, case_id, step, "51", "Page 5", "Fictional step 2",
                         evidence_source_id=b, evidence_location="Page 5")["replacement_claim_id"]
    final = restore_claim(connection, case_id, step, c2, "Fictional step 3",
                          evidence_source_id=a, evidence_location="Note 3")["replacement_claim_id"]

    chain = fetch_lineage(connection, case_id, c2)
    connection.rollback()
    assert [v["claim_id"] for v in chain["versions"]][-1] == final
    assert len(chain["transitions"]) == 3 and chain["problems"] == []
    # Versions keep the claim's own source; evidence sources vary.
    assert {v["source_id"] for v in chain["versions"]} == {b}
    assert [t["evidence_source_id"] for t in chain["transitions"]] == [a, b, a]

    v2 = snapshot_now(case_id)
    from src.report_renderer import snapshot_lineage
    diagnostics = snapshot_lineage(v2)
    assert (diagnostics["status"], diagnostics["lineages"], diagnostics["corrections"]) == (
        "NO_PROBLEMS_FOUND", 1, 3)


def test_identity_breaking_link_is_followed_and_reported(lineage_db):
    connection, fixtures, _, case_id, a, b, c1, c2 = lineage_db
    # Raw link from a 2025 claim of source b to a later 2024 claim of
    # source a: different source and year, which the application
    # would never write.
    other = fixtures.claim(a, 2024, "47")
    connection.commit()
    insert_raw_correction(c2, other)

    chain = fetch_lineage(connection, case_id, c2)
    connection.rollback()
    assert [v["claim_id"] for v in chain["versions"]] == [c2, other]
    assert any("changes the claim's" in p and "source" in p and "report year" in p
               for p in chain["problems"])

    diagnostics = fetch_case_diagnostics(connection, case_id)
    connection.rollback()
    assert diagnostics["status"] == "PROBLEMS_FOUND"


def test_cross_case_link_exposes_nothing_from_other_case(lineage_db):
    connection, fixtures, _, case_id, a, b, c1, c2 = lineage_db
    secret = connect()
    try:
        cursor = secret.cursor()
        cursor.execute("INSERT INTO investigation_cases (case_name) VALUES ('Other synthetic')")
        other_case = cursor.lastrowid
        cursor.execute("INSERT INTO case_sources (case_id, document_title) "
                       "VALUES (%s, 'Other case document')", (other_case,))
        other_source = cursor.lastrowid
        secret.commit()
        cursor.close()
    finally:
        secret.close()
    fixtures.source_ids.append(other_source)
    foreign_claim = fixtures.claim(other_source, 2025, "99")
    connection.commit()
    try:
        # A raw link into the other case, citing its document as evidence.
        insert_raw_correction(c2, foreign_claim, evidence_source=other_source)

        chain = fetch_lineage(connection, case_id, c2)
        connection.rollback()
        assert [v["claim_id"] for v in chain["versions"]] == [c2]
        assert any("missing claims" in p for p in chain["problems"])
        (transition,) = chain["transitions"]
        assert transition["evidence_document_title"] is None

        v2 = snapshot_now(case_id)
        assert foreign_claim not in {c["claim_id"] for c in v2["historical_claims"]}
        assert "Other case document" not in str(v2)
        # The other case's claim value and location are not read.
        (link,) = [c for c in v2["corrections"] if c["replacement_claim_id"] == foreign_claim]
        assert (link["replacement_value"], link["replacement_location"],
                link["replacement_fingerprint"]) == (None, None, None)
        assert "'99'" not in str(v2)
        from src.report_renderer import snapshot_lineage
        diagnostics = snapshot_lineage(v2)
        assert diagnostics["status"] == "PROBLEMS_FOUND"
        assert any("outside this investigation" in p for p in diagnostics["problems"])
        assert any(f"claim {foreign_claim} is referenced" in p for p in diagnostics["problems"])
    finally:
        cleanup = connect()
        try:
            cursor = cleanup.cursor()
            cursor.execute("SET @crie_claim_maintenance = 1")
            cursor.execute("DELETE FROM claim_corrections WHERE replacement_claim_id = %s",
                           (foreign_claim,))
            cursor.execute("SET @crie_claim_maintenance = 0")
            cursor.execute("DELETE FROM disclosure_claims WHERE source_id = %s", (other_source,))
            cursor.execute("DELETE FROM case_sources WHERE source_id = %s", (other_source,))
            cursor.execute("DELETE FROM investigation_cases WHERE case_id = %s", (other_case,))
            cleanup.commit()
            cursor.close()
        finally:
            cleanup.close()
        fixtures.source_ids.remove(other_source)
