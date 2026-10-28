
"""The 23B.2B-1 migration and its test and E2E twins (not executed here)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "sql/migrations/m23b2b_gleif_observations.sql"
TWINS = {
    "corporate_intelligence_test": ROOT / "sql/test_schema_upgrade_m23b2b.sql",
    "corporate_intelligence_e2e": ROOT / "sql/e2e/m23b2b_e2e_gleif_observations.sql",
}
NEW_TABLES = ("gleif_source_files", "gleif_import_batches", "relationship_observations",
              "relationship_observation_periods", "legal_entity_observations",
              "relationship_batch_observations", "entity_batch_observations")
FLAGGED_DELETE = re.compile(
    r"BEGIN\s+IF COALESCE\(@crie_gleif_maintenance, 0\) <> 1 THEN\s+"
    r"SIGNAL SQLSTATE '45000'\s+SET MESSAGE_TEXT = '(\w+ is append-only)';\s+END IF;\s+END\$\$")
TEST_TABLES = re.compile(
    r"CREATE TABLE IF NOT EXISTS corporate_intelligence_test\.(\w+) \(.*?\) ENGINE=InnoDB[^;]*;\s*",
    re.S)


def code(path):
    return "\n".join(line for line in path.read_text().splitlines()
                     if not line.lstrip().startswith("--")).strip()


def statements(text, keyword):
    return re.findall(rf"{keyword}\s+([\w.]+)", text)


def test_production_migration_is_additive_and_qualified():
    text = code(PRODUCTION)

    assert set(re.findall(r"corporate_intelligence\w*", text)) == {"corporate_intelligence"}
    names = re.findall(r"\b(?:TABLE|REFERENCES|(?:INSERT|UPDATE|DELETE) ON|TRIGGER|FROM|JOIN)\s+([A-Za-z_][\w.]*)", text)
    assert names and all(n.startswith("corporate_intelligence.") for n in names), names
    for forbidden in ("DROP ", "DELETE FROM", "INSERT INTO", "UPDATE corporate", "GRANT ", "USE ",
                      "maintenance", "MODIFY ", "CHANGE ", "RENAME ", "PRIMARY KEY (child",
                      "foreign_key_checks", "CREATE TABLE IF NOT EXISTS"):
        assert forbidden not in text, forbidden
    assert statements(text, "CREATE TABLE") == [f"corporate_intelligence.{t}" for t in NEW_TABLES]


def test_existing_relationship_anchor_is_only_extended():
    text = code(PRODUCTION)
    alters = re.findall(r"ALTER TABLE ([\w.]+)\s+(.*?);", text, re.S)

    assert [a[0] for a in alters] == ["corporate_intelligence.relationship_evidence"] * 2
    column, constraint = (" ".join(a[1].split()) for a in alters)
    assert column == "ADD COLUMN first_batch_id BIGINT UNSIGNED NULL DEFAULT NULL, ALGORITHM=INSTANT"
    assert constraint == ("ADD CONSTRAINT fk_evidence_first_batch FOREIGN KEY (first_batch_id) "
                          "REFERENCES corporate_intelligence.gleif_import_batches (batch_id), "
                          "ALGORITHM=COPY")
    # Nothing touches the tables that reference the anchor.
    for table in ("investigation_cases", "relationship_periods", "corporate_relationships ",
                  "legal_entities "):
        assert f"ALTER TABLE corporate_intelligence.{table}" not in text
    assert "fk_case_evidence" not in text and "fk_period_evidence" not in text
    assert "first_batch_id cannot change" in text
    assert "NOT (NEW.first_batch_id <=> OLD.first_batch_id)" in text


def test_identity_keys_and_matching_collations():
    text = " ".join(code(PRODUCTION).split())

    for key in (
        "UNIQUE KEY uq_gleif_source_file (dataset, file_sha256)",
        "UNIQUE KEY uq_gleif_batch_selection (source_file_id, selection_fingerprint, record_hash_version)",
        "UNIQUE KEY uq_relationship_observation_record (source_file_id, record_hash_version, "
        "child_lei, parent_lei, relationship_type, record_sha256)",
        "UNIQUE KEY uq_relationship_observation_ordinal (source_file_id, record_hash_version, "
        "source_record_ordinal)",
        "UNIQUE KEY uq_entity_observation_record (source_file_id, record_hash_version, lei, record_sha256)",
        "FOREIGN KEY (batch_id, source_file_id) REFERENCES "
        "corporate_intelligence.gleif_import_batches (batch_id, source_file_id)",
    ):
        assert key in text, key
    # File name and ContentDate are recorded but are never part of identity.
    assert "file_name" not in text.split("uq_gleif_source_file")[1].split(")")[0]
    # Foreign-key columns match the referenced production columns exactly.
    assert text.count("child_lei CHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL") == 1
    assert "relationship_type VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL" in text
    assert "lei CHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL" in text
    assert "content_date_utc DATETIME(3) NOT NULL" in text
    assert "legal_name VARCHAR(1000) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL" in text


def test_lifecycle_and_append_only_triggers():
    text = code(PRODUCTION)

    for message in ("a completed batch is immutable", "a batch identity cannot change",
                    "batch progress cannot go backwards", "only the lease holder may advance a batch",
                    "a batch is complete only when its selection is fully scanned",
                    "only the lease holder may complete a batch",
                    "a live lease cannot be taken over",
                    "a takeover resumes from its progress under a new lease",
                    "only the lease holder may fail a batch before its lease expires",
                    "a started batch needs a live lease of at most one hour",
                    "membership does not belong to the leased batch in progress",
                    "batch counts do not match its recorded observations",
                    "a failed batch resumes from its progress under a new lease",
                    "batch status transition not permitted",
                    "observation does not belong to the leased batch in progress",
                    "period does not belong to the leased batch in progress",
                    "anchor does not match the leased batch in progress"):
        assert message in text, message
    for table in NEW_TABLES:
        assert f"BEFORE DELETE ON corporate_intelligence.{table} FOR EACH ROW\n" \
               f"SIGNAL SQLSTATE '45000'" in text, table
    for table in NEW_TABLES[2:] + ("gleif_source_files",):
        assert f"BEFORE UPDATE ON corporate_intelligence.{table} FOR EACH ROW\n" \
               f"SIGNAL SQLSTATE '45000'" in text, table
    # Every lease comparison is binary and every row trigger requires an
    # unexpired lease.
    assert text.count("@crie_gleif_lease") == text.count("CAST(@crie_gleif_lease AS BINARY)") == 10
    assert text.count("b.lease_expires_at_utc > UTC_TIMESTAMP(6)") == 6
    assert "INSERT IGNORE" not in text


def test_twins_differ_only_by_database_flagged_delete_and_test_tables():
    production = code(PRODUCTION)
    for database, path in TWINS.items():
        twin = code(path)
        assert set(re.findall(r"corporate_intelligence\w*", twin)) == {database}
        assert len(FLAGGED_DELETE.findall(twin)) == 7
        twin = FLAGGED_DELETE.sub(
            lambda m: f"SIGNAL SQLSTATE '45000'\n    SET MESSAGE_TEXT = '{m.group(1)}'$$", twin)
        if database == "corporate_intelligence_test":
            assert TEST_TABLES.findall(twin) == ["corporate_relationships", "relationship_evidence"]
            twin = TEST_TABLES.sub("", twin)
        else:
            assert "CREATE TABLE IF NOT EXISTS" not in twin
        assert twin.replace(database, "corporate_intelligence") == production


def test_test_tables_match_production_definitions():
    twin = code(TWINS["corporate_intelligence_test"])
    blocks = {m.group(1): m.group(0) for m in TEST_TABLES.finditer(twin)}

    assert "PRIMARY KEY (`child_lei`,`parent_lei`,`relationship_type`,`snapshot_at_utc`)" \
        in blocks["relationship_evidence"]
    assert "CONSTRAINT `fk_evidence_relationship` FOREIGN KEY" in blocks["relationship_evidence"]
    assert "`snapshot_at_utc` datetime NOT NULL" in blocks["relationship_evidence"]
    assert "PRIMARY KEY (`child_lei`,`parent_lei`,`relationship_type`)" \
        in blocks["corporate_relationships"]
    assert all("COLLATE=utf8mb4_unicode_ci" in b for b in blocks.values())
    # The tables exist before the migration extends relationship_evidence.
    assert twin.index("relationship_evidence (") < twin.index("ALTER TABLE")


def test_membership_and_selection_completion():
    text = " ".join(code(PRODUCTION).split())

    for fragment in (
        "PRIMARY KEY (batch_id, observation_id)",
        "FOREIGN KEY (observation_id, source_file_id) REFERENCES "
        "corporate_intelligence.relationship_observations (observation_id, source_file_id)",
        "FOREIGN KEY (observation_id, source_file_id) REFERENCES "
        "corporate_intelligence.legal_entity_observations (observation_id, source_file_id)",
        "UNIQUE KEY uq_relationship_observation_file (observation_id, source_file_id)",
        "UNIQUE KEY uq_entity_observation_file (observation_id, source_file_id)",
        "WHEN 'FIRST_N' THEN NEW.records_scanned = LEAST(NEW.selection_limit, file_records)",
        "(selection_mode = 'LEI_SET' OR records_selected = records_scanned)",
        "(selection_limit IS NULL OR records_scanned <= selection_limit)",
        "JSON_UNQUOTE(JSON_EXTRACT(selection_json, '$.mode')) = (selection_mode COLLATE utf8mb4_bin)",
        "lease_expires_at_utc DATETIME(6) NOT NULL",
        "linked <> NEW.observations_inserted + NEW.observations_already_recorded",
    ):
        assert fragment in text, fragment
    # No CHECK uses the clock (MySQL forbids non-deterministic CHECKs).
    for check in re.findall(r"CHECK \((.*?)\)(?:,|\s*\) ENGINE)", text):
        assert "UTC_TIMESTAMP" not in check and "NOW(" not in check


def test_installation_check_is_read_only_and_lists_every_object():
    check = code(ROOT / "sql/m23b2b_installation_check.sql")
    production = code(PRODUCTION)

    statements = [s.strip().split()[0].upper() for s in check.split(";") if s.strip()]
    assert set(statements) <= {"SELECT", "SET", "PREPARE", "EXECUTE", "DEALLOCATE"}
    for forbidden in ("CREATE ", "ALTER ", "DROP ", "INSERT ", "UPDATE ", "DELETE ", "GRANT "):
        assert forbidden not in check.upper(), forbidden
    expected = re.findall(r"ROW\(\d+, '([A-Z ]+)', '([\w.]+)'\)", check)
    created = ([("TABLE", t) for t in re.findall(r"CREATE TABLE corporate_intelligence\.(\w+)", production)]
               + [("COLUMN", "relationship_evidence.first_batch_id"),
                  ("FOREIGN KEY", "relationship_evidence.fk_evidence_first_batch")]
               + [("TRIGGER", t) for t in re.findall(r"CREATE TRIGGER corporate_intelligence\.(\w+)",
                                                       production)])
    assert expected == created
