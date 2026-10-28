"""
Synthetic demonstration data for the CRIE walkthrough (README, "Demonstration").
End-to-end database (corporate_intelligence_e2e) only; no real GLEIF or company data.

    python scripts/demo_synthetic_case.py files <directory>
        Write four synthetic GLEIF files (LEI2 and RR, June and October),
        to be imported with src.gleif_import.
    python scripts/demo_synthetic_case.py claims --case-id N
        Load three synthetic claims onto the case's three registered sources
        (the dashboard has no claim-entry form; src.import_disclosure_claims
        is fixed to case 1).
    python scripts/demo_synthetic_case.py register --case-id N --run-id R --runs-root DIR
        Register a generated run in the E2E report registry
        (src.register_report_run accepts only the test and production databases).
    python scripts/demo_synthetic_case.py cleanup
        Delete every demonstration row (LEIs SYNTHDEMO*, files synthetic-demo-*).
    python scripts/demo_synthetic_case.py cleanup-guests --older-than-hours N
        Delete guest sandboxes' investigations opened more than N hours ago,
        with everything recorded for them. The published example is not a
        guest's and is never touched.
    python scripts/demo_synthetic_case.py build --runs-root DIR
        Build the whole demonstration case in an empty demonstration database:
        the README walkthrough's steps, recorded with the importer and the
        same functions and statements the dashboard uses, then its version 4
        report, written under DIR and registered.
    python scripts/demo_synthetic_case.py schema OUTPUT
        Write the demonstration database schema for a hosted MySQL server:
        the end-to-end schema scripts in order, without the view's
        root@localhost DEFINER clause. Nothing is executed.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

E2E_DATABASE = "corporate_intelligence_e2e"
PREFIX = "SYNTHDEMO"
DEMO_OWNER = "crie:demonstration"         # a system owner no user can sign in as
PUBLICATION_TITLE = "Synthetic example: a reported consolidation and two years of disclosures"
PUBLICATION_SUMMARY = (
    "A prepared example built from synthetic companies, documents and GLEIF files.\n\n"
    "- GLEIF reports Synthetic Harbour Holdings plc as directly consolidated by Synthetic "
    "Parent Group AG, with the validation class Entity-supplied only. Accounting "
    "consolidation is not evidence of ownership or control.\n"
    "- The 2024 interest was first transcribed as 06 and corrected to 60 from page 12, "
    "note 3 of the annual report; both versions stay recorded.\n"
    "- The 2025 value of 60 appears in the regulator filing and in a newswire article that "
    "republishes it, so their agreement is recorded as related sources, not corroboration.\n"
    "- The claims' subject was first recorded as ambiguous between two LEIs, then mapped to "
    "Synthetic Harbour Holdings plc.\n\n"
    "The example records no conclusion about the relationship itself."
)
ENTITY, METRIC = "Synthetic Harbour Holdings plc", "parent_interest_pct"


def connect():
    import mysql.connector
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    connection = mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"), port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"), password=os.getenv("MYSQL_PASSWORD"),
        database=E2E_DATABASE, time_zone="+00:00")
    cursor = connection.cursor()
    cursor.execute("SELECT DATABASE()")
    if cursor.fetchone()[0] != E2E_DATABASE:
        raise SystemExit(f"Refused: not connected to {E2E_DATABASE}")
    cursor.close()
    connection.rollback()
    return connection


def write_files(directory):
    from gleif_lei2_samples import record, write as write_lei2
    from gleif_rr_samples import relationship, synthetic_lei, write as write_rr

    directory.mkdir(parents=True, exist_ok=True)
    harbour, parent, ultimate = (synthetic_lei(PREFIX + x) for x in "HPU")
    invalid = harbour[:-2] + f"{(int(harbour[-2:]) + 1) % 100:02d}"   # wrong check digits
    june, october = "2026-06-01T09:00:00Z", "2026-10-01T09:00:00Z"
    files = [
        write_lei2(directory / "synthetic-demo-lei2-june.xml", [
            record(lei=harbour, name=ENTITY, name_lang="en", jurisdiction="GB"),
            record(lei=parent, name="Synthetic Parent Group AG"),
            record(lei=ultimate, name="Synthetic Ultimate Holdings SA", jurisdiction="LU"),
            record(lei=invalid, name="Synthetic Unverifiable Ltd", name_lang="en", jurisdiction="GB"),
        ], content_date=june),
        write_rr(directory / "synthetic-demo-rr-june.xml", [
            relationship(harbour, parent),
            relationship(harbour, ultimate, kind="IS_ULTIMATELY_CONSOLIDATED_BY"),
            relationship(invalid, parent),
        ], content_date=june),
        write_lei2(directory / "synthetic-demo-lei2-october.xml", [
            record(lei=harbour, name="Synthetic Harbour Group plc", name_lang="en", jurisdiction="GB"),
            record(lei=parent, name="Synthetic Parent Group AG"),
        ], content_date=october),
        write_rr(directory / "synthetic-demo-rr-october.xml", [
            relationship(harbour, parent, last_update="2026-09-20T10:00:00Z", periods=(
                ("ACCOUNTING_PERIOD", "2025-01-01T00:00:00Z", "2025-12-31T00:00:00Z"),
                ("RELATIONSHIP_PERIOD", "2019-05-01T00:00:00Z", None))),
        ], content_date=october),
    ]
    for path in files:                      # import in this order
        print(path)
    print(f"Synthetic LEIs: {harbour} (reporting entity), {parent}, {ultimate}; "
          f"{invalid} has wrong check digits and is quarantined.")


def require_demo_case(connection, case_id):
    """Refuse any case not anchored on a demonstration LEI; cleanup removes only those."""

    cursor = connection.cursor()
    try:
        cursor.execute("SELECT child_lei FROM investigation_cases WHERE case_id = %s", (case_id,))
        row = cursor.fetchone()
    finally:
        cursor.close()
    connection.rollback()
    if not row or not row[0] or not row[0].startswith(PREFIX):
        connection.close()
        raise SystemExit("Refused: not a demonstration case.")


def load_claims(case_id):
    from src.disclosure_conflicts import normalize_claim

    connection = connect()
    require_demo_case(connection, case_id)
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT source_id FROM case_sources WHERE case_id = %s ORDER BY source_id",
                       (case_id,))
        sources = [r[0] for r in cursor.fetchall()]
        cursor.execute("SELECT COUNT(*) FROM disclosure_claims c JOIN case_sources s "
                       "USING (source_id) WHERE s.case_id = %s", (case_id,))
        if len(sources) != 3 or cursor.fetchone()[0]:
            raise SystemExit("Refused: the case needs exactly three registered sources and no claims.")
        claims = [(sources[0], 2024, "2024-12-31", "06", "Page 12"),   # deliberate transcription error
                  (sources[1], 2025, "2025-12-31", "60", "Section 4"),
                  (sources[2], 2025, "2025-12-31", "60", "Paragraph 2")]
        for source_id, year, period, value, location in claims:
            # Fingerprint as src.import_disclosure_claims computes it.
            fingerprint = hashlib.sha256(json.dumps(
                [source_id, ENTITY, METRIC, year, period, value, location],
                ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
            cursor.execute(
                "INSERT INTO disclosure_claims (source_id, entity_name, metric, report_year, "
                "period_end, value_text, normalized_value, source_location, claim_fingerprint) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (source_id, ENTITY, METRIC, year, period, value, normalize_claim(METRIC, value),
                 location, fingerprint))
        connection.commit()
        print(f"Loaded 3 claims onto sources {sources}.")
    finally:
        cursor.close()
        connection.close()


def register(case_id, run_id, runs_root):
    from src.report_registry import register_run

    connection = connect()
    require_demo_case(connection, case_id)
    try:
        register_run(connection, case_id, run_id, runs_root)
        print(f"Registered {run_id} in {E2E_DATABASE}.")
    finally:
        connection.close()


FLAGS = ("registry", "mapping", "assessment", "claim", "gleif", "publication")


def _case_statements(case_ids):
    """Deletes of these cases and everything recorded for them, in foreign-key order."""

    if not case_ids:
        return []
    cases = ", ".join(str(int(i)) for i in case_ids)
    sources = f"SELECT source_id FROM case_sources WHERE case_id IN ({cases})"
    runs = f"SELECT run_id FROM report_runs WHERE case_id IN ({cases})"
    return [
        # Test and E2E schemas allow these deletions only under explicit session flags.
        ("PUBLICATIONS", cases),
        "SET @crie_registry_maintenance = 1",
        f"DELETE FROM report_run_claims WHERE run_id IN ({runs})",
        f"DELETE FROM report_run_sources WHERE run_id IN ({runs})",
        f"DELETE FROM report_runs WHERE case_id IN ({cases})",
        "SET @crie_mapping_maintenance = 1",
        f"DELETE FROM claim_subject_mappings WHERE case_id IN ({cases}) ORDER BY mapping_id DESC",
        "SET @crie_assessment_maintenance = 1",
        f"DELETE FROM source_relationship_assessments WHERE case_id IN ({cases}) "
        "ORDER BY assessment_id DESC",
        "SET @crie_claim_maintenance = 1",
        "DELETE cc FROM claim_corrections cc JOIN disclosure_claims c "
        f"ON c.claim_id = cc.original_claim_id WHERE c.source_id IN ({sources})",
        f"DELETE FROM disclosure_claims WHERE source_id IN ({sources}) ORDER BY claim_id DESC",
        "DELETE a FROM source_history_annotations a JOIN source_assessment_history h "
        f"USING (history_id) WHERE h.source_id IN ({sources})",
        f"DELETE FROM source_assessment_history WHERE source_id IN ({sources})",
        f"DELETE FROM source_metadata_history WHERE source_id IN ({sources})",
        f"DELETE FROM case_sources WHERE case_id IN ({cases})",
        f"DELETE FROM case_notes WHERE case_id IN ({cases})",
        f"DELETE FROM investigation_cases WHERE case_id IN ({cases})",
    ]


def _execute_all(statements, describe):
    from test_mysql_gleif_import import purge_m23b2b3_rows

    connection = connect()
    cursor = connection.cursor()
    try:
        removed = 0
        for statement in statements:
            if isinstance(statement, tuple) and statement[0] == "PURGE":
                purge_m23b2b3_rows(cursor, statement[1])
                continue
            if isinstance(statement, tuple) and statement[0] == "PUBLICATIONS":
                cursor.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE "
                               "TABLE_SCHEMA = DATABASE() "
                               "AND TABLE_NAME = 'investigation_publications'")
                if cursor.fetchone()[0]:
                    cursor.execute("SET @crie_publication_maintenance = 1")
                    cursor.execute("DELETE FROM investigation_publications "
                                   f"WHERE case_id IN ({statement[1]})")
                    removed += max(cursor.rowcount, 0)
                continue
            cursor.execute(statement)
            removed += max(cursor.rowcount, 0) if statement.startswith("DELETE") else 0
        for flag in FLAGS:
            cursor.execute(f"SET @crie_{flag}_maintenance = 0")
        connection.commit()
        print(f"Removed {removed} {describe}.")
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def _case_ids(where, params=()):
    connection = connect()
    cursor = connection.cursor()
    try:
        cursor.execute(f"SELECT case_id FROM investigation_cases WHERE {where}", params)
        return [row[0] for row in cursor.fetchall()]
    finally:
        cursor.close()
        connection.rollback()
        connection.close()


def cleanup():
    """Delete every demonstration row: cases on SYNTHDEMO entities, whoever owns them, then
    the synthetic GLEIF records."""

    files = "f.file_name LIKE 'synthetic-demo-%'"
    statements = _case_statements(_case_ids("child_lei LIKE 'SYNTHDEMO%'")) + [
        "SET @crie_gleif_maintenance = 1",
        ("PURGE", files),                                      # GLEIF quarantine and positions
        *[f"DELETE m FROM {table} m JOIN gleif_source_files f USING (source_file_id) WHERE {files}"
          for table in ("relationship_batch_observations", "entity_batch_observations")],
        "DELETE p FROM relationship_observation_periods p JOIN relationship_observations o "
        f"USING (observation_id) JOIN gleif_source_files f USING (source_file_id) WHERE {files}",
        *[f"DELETE o FROM {table} o JOIN gleif_source_files f USING (source_file_id) WHERE {files}"
          for table in ("relationship_observations", "legal_entity_observations")],
        "DELETE FROM relationship_periods WHERE child_lei LIKE 'SYNTHDEMO%'",
        "DELETE FROM relationship_evidence WHERE child_lei LIKE 'SYNTHDEMO%'",
        "DELETE b FROM gleif_import_batches b JOIN gleif_source_files f "
        f"USING (source_file_id) WHERE {files}",
        "DELETE FROM gleif_source_files WHERE file_name LIKE 'synthetic-demo-%'",
        "DELETE FROM corporate_relationships WHERE child_lei LIKE 'SYNTHDEMO%' "
        "OR parent_lei LIKE 'SYNTHDEMO%'",
        "DELETE FROM legal_entities WHERE lei LIKE 'SYNTHDEMO%'",
    ]
    _execute_all(statements, "demonstration rows (plus GLEIF quarantine and position rows)")


def cleanup_guests(older_than_hours):
    """Delete guest sandboxes' investigations opened more than the given hours ago."""

    if older_than_hours < 1:
        raise SystemExit("Refused: keep sandboxes for at least one hour.")
    connection = connect()
    installed = _ownership_installed(connection)
    connection.close()
    if not installed:
        raise SystemExit("This database records no investigation owners, so it has no "
                         "guest sandboxes.")
    ids = _case_ids("owner_subject LIKE 'guest:%%' "
                    "AND created_at < UTC_TIMESTAMP() - INTERVAL %s HOUR", (older_than_hours,))
    _execute_all(_case_statements(ids), f"rows of {len(ids)} guest investigation(s)")


SOURCES = [
    ("Synthetic Harbour Holdings plc annual report 2024", "Synthetic Harbour Holdings plc",
     "https://example.invalid/harbour/annual-report-2024.pdf", "2025-03-31"),
    ("Synthetic Markets Regulator filing 2025", "Synthetic Markets Regulator",
     "https://example.invalid/regulator/filing-2025.pdf", "2026-02-15"),
    ("Synthetic Newswire article on the 2025 filing", "Synthetic Newswire",
     "https://example.invalid/newswire/harbour-2025", "2026-02-16"),
]


def _execute(sql, params=()):
    connection = connect()
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        connection.commit()
        return cursor.lastrowid
    finally:
        cursor.close()
        connection.close()


def build(runs_root):
    """The README walkthrough, recorded in an empty demonstration database."""

    from src.claim_corrections import correct_claim
    from src.claim_subjects import record_mapping
    from src.evidence_snapshot import load_evidence_snapshot
    from src.generate_versioned_report import generate_versioned_report
    from src.gleif_import import import_lei2_file, import_rr_file
    from src.gleif_observations import canonical_selection
    from src.relationship_context import load_relationship_context
    from src.source_assessments import record_assessment

    connection = connect()
    cursor = connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM legal_entities WHERE lei LIKE 'SYNTHDEMO%'")
    present = cursor.fetchone()[0]
    cursor.close()
    connection.close()
    if present:
        raise SystemExit("Refused: demonstration rows already exist; run cleanup first.")

    # 1. The GLEIF files, imported in content-date order.
    with tempfile.TemporaryDirectory() as directory:
        write_files(Path(directory))
        for name, dataset in (("lei2-june", "LEI2"), ("rr-june", "RR"),
                              ("lei2-october", "LEI2"), ("rr-october", "RR")):
            importer = import_lei2_file if dataset == "LEI2" else import_rr_file
            summary = importer(connect, Path(directory) / f"synthetic-demo-{name}.xml",
                               canonical_selection(dataset, "ALL_RECORDS"),
                               started_by="CRIE demonstration build")
            if summary["status"] != "COMPLETED":
                raise SystemExit(f"Import of {name} did not complete: {summary['status']}")

    # 2. The investigation, on the relationship's current evidence row (as the case form),
    #    owned by the demonstration's system owner where cases have owners.
    connection = connect()
    relationships, _, _ = load_relationship_context(connection)
    owned = _ownership_installed(connection)
    connection.close()
    anchor = next(r for r in relationships
                  if r["child_name"] == ENTITY and r["parent_name"] == "Synthetic Parent Group AG")
    key = (anchor["child_lei"], anchor["parent_lei"], anchor["relationship_type"],
           anchor["snapshot_at_utc"])
    if owned:
        case_id = _execute(
            "INSERT INTO investigation_cases (child_lei, parent_lei, relationship_type, "
            "snapshot_at_utc, review_status, assessment, owner_subject) "
            "VALUES (%s, %s, %s, %s, 'OPEN', 'NOT_ASSESSED', %s)", (*key, DEMO_OWNER))
    else:
        case_id = _execute(
            "INSERT INTO investigation_cases (child_lei, parent_lei, relationship_type, "
            "snapshot_at_utc, review_status, assessment) "
            "VALUES (%s, %s, %s, %s, 'OPEN', 'NOT_ASSESSED')", key)
    _execute("INSERT INTO case_notes (case_id, note_text, source_url) VALUES (%s, %s, NULL)",
             (case_id, "Demonstration case: test whether the reported consolidation and the "
                       "2024 and 2025 disclosures of the parent's interest are supported by "
                       "independent sources."))

    # 3. Three registered sources (as the registration form) and their claims.
    sources = []
    for title, publisher, url, date in SOURCES:
        connection = connect()
        cursor = connection.cursor()
        cursor.execute(
            "INSERT INTO case_sources (case_id, document_title, publisher, source_url, "
            "publication_date, evidence_assessment, analyst_notes) "
            "VALUES (%s, %s, %s, %s, %s, 'NOT_REVIEWED', NULL)",
            (case_id, title, publisher, url, date))
        sources.append(cursor.lastrowid)
        cursor.execute(
            "INSERT INTO source_assessment_history (source_id, event_type, previous_assessment, "
            "new_assessment, review_reason) VALUES (%s, 'CREATED', NULL, 'NOT_REVIEWED', %s)",
            (sources[-1], "Source initially registered."))
        connection.commit()
        cursor.close()
        connection.close()
    annual, regulator, newswire = sources
    load_claims(case_id)

    # 4. A review of the regulator filing (as the review form).
    connection = connect()
    cursor = connection.cursor()
    cursor.execute(
        "INSERT INTO source_assessment_history (source_id, event_type, previous_assessment, "
        "new_assessment, review_reason) VALUES (%s, 'REVIEW', 'NOT_REVIEWED', 'SUPPORTS', %s)",
        (regulator, "Section 4 states the 60% interest for 2025; the filing is the regulator's "
                    "own record."))
    cursor.execute("UPDATE case_sources SET evidence_assessment = 'SUPPORTS' WHERE source_id = %s",
                   (regulator,))
    cursor.execute("SELECT claim_id FROM disclosure_claims WHERE source_id = %s", (annual,))
    mistranscribed = cursor.fetchone()[0]
    connection.commit()
    cursor.close()
    connection.close()

    # 5. The correction, the newswire's dependence and the subject's identity.
    connection = connect()
    correct_claim(connection, case_id, mistranscribed, "60", "Page 12, note 3",
                  "Transcribed as 06; page 12 note 3 reads 60%.",
                  evidence_source_id=annual, evidence_location="Page 12, note 3")
    connection.close()
    connection = connect()
    record_assessment(connection, case_id, source_a_id=regulator, source_b_id=newswire,
                      relationship="REPUBLICATION", basis="DOCUMENTED",
                      rationale="The article states that it reports the regulator filing.",
                      limitations=None, recorded_by="CRIE demonstration",
                      evidence_source_id=newswire, evidence_reference=None,
                      evidence_location="Paragraph 1")
    connection.close()
    harbour = anchor["child_lei"]
    ultimate = next(r["parent_lei"] for r in relationships
                    if r["child_lei"] == harbour and r["parent_lei"] != anchor["parent_lei"])
    common = dict(subject_text=ENTITY, recorded_by="CRIE demonstration", basis="DOCUMENTED",
                  evidence_reference="Synthetic LEI register",
                  evidence_location=f"Entry for {harbour}")
    connection = connect()
    first = record_mapping(connection, case_id, status="AMBIGUOUS",
                           candidate_leis=[harbour, ultimate],
                           rationale="Two registered entities could match the subject text.",
                           **common)
    connection.close()
    connection = connect()
    record_mapping(connection, case_id, status="MAPPED", lei=harbour, name_type="LEGAL_NAME",
                   rationale="The register entry matches the legal name and jurisdiction.",
                   limitations="Synthetic register only; says nothing about ownership or control.",
                   supersedes_mapping_id=first["mapping_id"], **common)
    connection.close()

    # 6. The version 4 report, read from this database only, then registered.
    connection = connect()
    try:
        connection.start_transaction(consistent_snapshot=True, isolation_level="REPEATABLE READ",
                                     readonly=True)
        snapshot = load_evidence_snapshot(case_id=case_id, years=(2024, 2025),
                                          connection=connection, version="4")
        connection.rollback()
    finally:
        connection.close()
    run = generate_versioned_report(case_id=case_id, previous_year=2024, current_year=2025,
                                    output_root=runs_root, snapshot=snapshot)
    register(case_id, run["run_id"], runs_root)
    if owned:
        _execute("INSERT INTO investigation_publications (case_id, run_id, title, summary, "
                 "author_name, published_by) VALUES (%s, %s, %s, %s, %s, %s)",
                 (case_id, run["run_id"], PUBLICATION_TITLE, PUBLICATION_SUMMARY,
                  "CRIE demonstration", DEMO_OWNER))
    print(f"Demonstration case #{case_id} built; report {run['run_id']} in {run['run_dir']}"
          + ("; published." if owned else "."))


def _ownership_installed(connection):
    cursor = connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = "
                   "DATABASE() AND TABLE_NAME = 'investigation_cases' "
                   "AND COLUMN_NAME = 'owner_subject'")
    installed = cursor.fetchone()[0] == 1
    cursor.close()
    connection.rollback()
    return installed


SCHEMA_SCRIPTS = (
    "m23a_e2e_02_schema.sql", "m23a_e2e_03_registry.sql", "m23a_e2e_04_lineage.sql",
    "m23b1_e2e_source_assessments.sql", "m23b1_e2e_fix_supersession_trigger.sql",
    "m23b2a_e2e_claim_subject_mappings.sql", "m23b2b_e2e_gleif_observations.sql",
    "m23b2b3_e2e_gleif_quarantine.sql", "m24_e2e_investigation_ownership.sql",
)
DEFINER_LINE = re.compile(r"^/\*!50013 DEFINER=`root`@`localhost` SQL SECURITY DEFINER \*/$")


def schema_bundle():
    """The end-to-end schema for a hosted server: the view is owned by whoever installs it."""

    parts = ["-- CRIE demonstration database schema (corporate_intelligence_e2e).\n"
             "-- Generated by scripts/demo_synthetic_case.py schema from sql/e2e/, in order;\n"
             "-- the only change is that the view's DEFINER=root@localhost clause is dropped,\n"
             "-- so the installing account owns it. Run with the mysql client, without --force,\n"
             "-- as an account allowed to create tables, views and triggers.\n"]
    dropped = 0
    for name in SCHEMA_SCRIPTS:
        lines = (ROOT / "sql/e2e" / name).read_text("utf-8").splitlines()
        kept = [line for line in lines if not DEFINER_LINE.match(line)]
        dropped += len(lines) - len(kept)
        parts.append(f"\n-- ===== {name} =====\n" + "\n".join(kept) + "\n")
    bundle = "".join(parts)
    if dropped != 1 or "DEFINER" in re.sub(r"--[^\n]*", "", bundle):
        raise SystemExit("Refused: the schema scripts' DEFINER clauses are not as expected.")
    return bundle


def write_schema(output):
    if output.exists():
        raise SystemExit(f"Refused: {output} exists.")
    output.write_text(schema_bundle(), "utf-8")
    print(f"Wrote {output}.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("files").add_argument("directory", type=Path)
    claims = commands.add_parser("claims")
    claims.add_argument("--case-id", type=int, required=True)
    registration = commands.add_parser("register")
    registration.add_argument("--case-id", type=int, required=True)
    registration.add_argument("--run-id", required=True)
    registration.add_argument("--runs-root", type=Path, required=True)
    commands.add_parser("cleanup")
    commands.add_parser("cleanup-guests").add_argument("--older-than-hours", type=int,
                                                       required=True)
    commands.add_parser("build").add_argument("--runs-root", type=Path, required=True)
    commands.add_parser("schema").add_argument("output", type=Path)
    args = parser.parse_args(argv)

    if args.command == "files":
        write_files(args.directory)
    elif args.command == "claims":
        load_claims(args.case_id)
    elif args.command == "register":
        register(args.case_id, args.run_id, args.runs_root)
    elif args.command == "build":
        build(args.runs_root)
    elif args.command == "schema":
        write_schema(args.output)
    elif args.command == "cleanup-guests":
        cleanup_guests(args.older_than_hours)
    else:
        cleanup()


if __name__ == "__main__":
    main()
