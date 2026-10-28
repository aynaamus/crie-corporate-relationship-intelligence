
"""The 23B.2B-3a-2 migration, its twins and checker (static; nothing is executed)."""

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "sql/migrations/m23b2b3_gleif_quarantine.sql"
APPLIED = ROOT / "sql/migrations/m23b2b_gleif_observations.sql"
TWINS = {
    "corporate_intelligence_test": ROOT / "sql/test_schema_upgrade_m23b2b3.sql",
    "corporate_intelligence_e2e": ROOT / "sql/e2e/m23b2b3_e2e_gleif_quarantine.sql",
}
CHECKER = ROOT / "sql/m23b2b3_installation_check.sql"
# The applied 23B.2B-1 scripts, byte for byte as installed.
APPLIED_SHA256 = {
    "sql/migrations/m23b2b_gleif_observations.sql":
        "961434673f689a9cfd9e883afcf2c3dc8d9d2fa53ef300e34fefcf32cad034ee",
    "sql/test_schema_upgrade_m23b2b.sql":
        "60464811762a243d4606b1c791942a08284e10b109e925606074b7d6efbf33be",
    "sql/e2e/m23b2b_e2e_gleif_observations.sql":
        "87bf856ba1d0a60c30449eab72626e69b499e6d7d476eb4ed853ac58cbe91001",
    "sql/m23b2b_installation_check.sql":
        "ffe02ca91c9e67c84b889be1e406bbd529950f7555005a41b15196ba063d1fe2",
}
NEW_TABLES = ("gleif_quarantined_records", "gleif_batch_quarantined_records",
              "gleif_batch_duplicate_occurrences", "gleif_source_positions", "gleif_batch_positions")
FLAGGED_DELETE = re.compile(
    r"BEGIN\s+IF COALESCE\(@crie_gleif_maintenance, 0\) <> 1 THEN\s+"
    r"SIGNAL SQLSTATE '45000'\s+SET MESSAGE_TEXT = '(\w+ is append-only)';\s+END IF;\s+END\$\$")


def code(path):
    return "\n".join(line for line in path.read_text().splitlines()
                     if not line.lstrip().startswith("--")).strip()


def flat(text):
    return " ".join(text.split())


def trigger(text, name):
    return re.search(rf"CREATE TRIGGER corporate_intelligence\.{name}\n.*?END\$\$", text, re.S).group(0)


def test_applied_migrations_are_unchanged():
    for name, digest in APPLIED_SHA256.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name


def test_production_migration_is_qualified_and_changes_only_what_is_named():
    text = code(PRODUCTION)
    # Statements the migration runs itself (trigger bodies run only on later writes).
    statements = re.sub(r"DELIMITER \$\$.*?DELIMITER ;", "", text, flags=re.S)
    triggers = text[text.index("DELIMITER $$"):text.index("DELIMITER ;")]

    assert set(re.findall(r"corporate_intelligence\w*", text)) == {"corporate_intelligence"}
    for forbidden in ("INSERT INTO", "UPDATE corporate", "DELETE FROM", "TRUNCATE"):
        assert forbidden not in statements, forbidden
    # Inside triggers, the only writes are the seven position registrations.
    assert re.findall(r"INSERT INTO corporate_intelligence\.(\w+)", triggers) == \
        ["gleif_source_positions"] * 3 + ["gleif_batch_positions"] * 4
    assert "UPDATE corporate" not in triggers and "DELETE FROM" not in triggers
    for forbidden in ("GRANT ",
                      "USE ", "maintenance", "foreign_key_checks", "DROP TABLE", "DROP COLUMN",
                      "MODIFY ", "CHANGE ", "RENAME ", "DROP INDEX", "DROP FOREIGN KEY"):
        assert forbidden not in text, forbidden
    # The only removals: the two replaced triggers and the replaced CHECK.
    assert re.findall(r"DROP (?:TRIGGER|CHECK) ([\w.]+)", text) == [
        "corporate_intelligence.trg_gleif_batches_start",
        "corporate_intelligence.trg_gleif_batches_lifecycle", "chk_gleif_batch_counts"]
    # Nothing in this migration refers to trusted identity tables.
    assert "legal_entities" not in text.replace("legal_entity_observations", "")
    assert "corporate_relationships" not in text and "relationship_evidence" not in text


def test_replacements_exist_before_their_originals_are_removed():
    text = code(PRODUCTION)

    assert text.index("ADD CONSTRAINT chk_gleif_batch_counts_v2") < text.index(
        "DROP CHECK chk_gleif_batch_counts")
    for original in ("trg_gleif_batches_start", "trg_gleif_batches_lifecycle"):
        replacement = trigger(text, original + "_v2")
        assert f"FOLLOWS {original}\n" in replacement
        assert text.index(replacement) < text.index(
            f"DROP TRIGGER corporate_intelligence.{original};")
    for table, follows in (("relationship_observations", "trg_relationship_observations_batch"),
                           ("legal_entity_observations", "trg_entity_observations_batch")):
        assert f"ON corporate_intelligence.{table} FOR EACH ROW\nFOLLOWS {follows}\n" in text


def test_replacement_triggers_keep_every_original_clause_in_order():
    """
    Superset: apart from its name and FOLLOWS clause, each replacement is
    its original with insertions only (a token-level diff deletes and
    replaces nothing), so every original rule is still enforced.
    """

    import difflib

    applied, text = code(APPLIED), code(PRODUCTION)
    for name in ("trg_gleif_batches_start", "trg_gleif_batches_lifecycle"):
        tokens = re.compile(r"\w+|[^\w\s]")                 # punctuation separately
        original = tokens.findall("\n".join(trigger(applied, name).splitlines()[2:]))
        replacement = tokens.findall("\n".join(trigger(text, name + "_v2").splitlines()[3:]))
        operations = {op for op, *_ in difflib.SequenceMatcher(
            None, original, replacement, autojunk=False).get_opcodes()}
        assert operations == {"equal", "insert"}, (name, operations)
    lifecycle = flat(trigger(text, "trg_gleif_batches_lifecycle_v2"))
    for clause in (
        "OR NEW.quarantined_inserted < OLD.quarantined_inserted",
        "OR NEW.quarantined_already_recorded < OLD.quarantined_already_recorded THEN",
        "AND NEW.quarantined_inserted <=> OLD.quarantined_inserted",
        "AND NEW.quarantined_already_recorded <=> OLD.quarantined_already_recorded;",
        "IF quarantined <> NEW.quarantined_inserted OR quarantined_linked <> quarantined "
        "OR quarantined_members <> NEW.quarantined_inserted + NEW.quarantined_already_recorded",
        "IF duplicates <> NEW.identical_duplicates THEN",
    ):
        assert clause in lifecycle, clause
    assert "OR NEW.quarantined_inserted <> 0 OR NEW.quarantined_already_recorded <> 0" in flat(
        trigger(text, "trg_gleif_batches_start_v2"))


def test_counts_check_original_and_replacement():
    applied, text = flat(code(APPLIED)), flat(code(PRODUCTION))
    original = ("CHECK (records_selected <= records_scanned AND observations_inserted + "
                "observations_already_recorded + identical_duplicates = records_selected AND "
                "(selection_mode = 'LEI_SET' OR records_selected = records_scanned) AND "
                "(selection_limit IS NULL OR records_scanned <= selection_limit))")
    replacement = ("CHECK (records_selected <= records_scanned AND observations_inserted + "
                   "observations_already_recorded + quarantined_inserted + "
                   "quarantined_already_recorded + identical_duplicates = records_selected AND "
                   "(selection_mode = 'LEI_SET' OR records_selected = records_scanned) AND "
                   "(selection_limit IS NULL OR records_scanned <= selection_limit))")
    assert f"CONSTRAINT chk_gleif_batch_counts {original}" in applied
    assert f"ADD CONSTRAINT chk_gleif_batch_counts_v2 {replacement}" in text
    # With quarantine counters at 0 the two rules are the same rule.
    assert replacement.replace("quarantined_inserted + quarantined_already_recorded + ", "") == original


def test_new_tables_keys_and_no_trusted_references():
    text = flat(code(PRODUCTION))

    for fragment in (
        "UNIQUE KEY uq_quarantine_record (source_file_id, record_hash_version, subject_key, record_sha256)",
        "UNIQUE KEY uq_quarantine_ordinal (source_file_id, record_hash_version, source_record_ordinal)",
        "PRIMARY KEY (batch_id, quarantine_id)",
        "UNIQUE KEY uq_quarantine_batch_ordinal (batch_id, source_record_ordinal)",
        "FOREIGN KEY (quarantine_id, source_file_id) REFERENCES "
        "corporate_intelligence.gleif_quarantined_records (quarantine_id, source_file_id)",
        "PRIMARY KEY (batch_id, source_record_ordinal)",
        "CHECK (repeats_ordinal >= 1 AND repeats_ordinal < source_record_ordinal)",
        "subject_key VARCHAR(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL",
    ):
        assert fragment in text, fragment
    references = re.findall(r"REFERENCES corporate_intelligence\.(\w+)", text)
    assert set(references) == {"gleif_import_batches", "gleif_quarantined_records",
                               "gleif_source_files"}


def test_lei2_and_source_file_column_definitions():
    text = flat(code(PRODUCTION))

    for fragment in (
        "ADD COLUMN originator CHAR(20) CHARACTER SET ascii COLLATE ascii_bin NULL",
        "ADD COLUMN header_extension JSON NULL",
        "ADD COLUMN header_source_counts_consistent BOOLEAN NULL",
        "CHECK ((header_extension IS NULL) = (header_source_counts_consistent IS NULL)",
        "ADD COLUMN canonical_record JSON NULL",
        "ADD COLUMN entity_category VARCHAR(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL",
        "ADD COLUMN entity_sub_category VARCHAR(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL",
        "ADD COLUMN entity_created_at_utc DATETIME(3) NULL",
        "ADD COLUMN validation_sources VARCHAR(40) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NULL",
        "ADD COLUMN identifier_check ENUM('VALID', 'SECONDARY_IDENTIFIER_FLAGGED') NULL",
        "CHECK (canonical_record IS NOT NULL AND JSON_TYPE(canonical_record) = 'OBJECT'",
        "ADD COLUMN quarantined_inserted BIGINT UNSIGNED NOT NULL DEFAULT 0",
        "ADD COLUMN quarantined_already_recorded BIGINT UNSIGNED NOT NULL DEFAULT 0",
    ):
        assert fragment in text, fragment


def test_new_tables_are_append_only_and_lease_checked():
    text = code(PRODUCTION)

    for table in NEW_TABLES:
        for event in ("UPDATE", "DELETE"):
            assert f"BEFORE {event} ON corporate_intelligence.{table} FOR EACH ROW\n" \
                   "SIGNAL SQLSTATE '45000'" in text, (table, event)
    assert text.count("b.lease_token <=> CAST(@crie_gleif_lease AS BINARY)") == 3
    assert text.count("b.lease_expires_at_utc > UTC_TIMESTAMP(6)") == 3
    assert text.count("'a source position cannot be both an observation and quarantined'") == 3


def test_twins_differ_only_by_database_and_flagged_deletes():
    production = code(PRODUCTION)
    for database, path in TWINS.items():
        twin = code(path)
        assert set(re.findall(r"corporate_intelligence\w*", twin)) == {database}
        assert len(FLAGGED_DELETE.findall(twin)) == 5
        twin = FLAGGED_DELETE.sub(
            lambda m: f"SIGNAL SQLSTATE '45000'\n    SET MESSAGE_TEXT = '{m.group(1)}'$$", twin)
        assert twin.replace(database, "corporate_intelligence") == production


def test_checker_is_read_only_and_lists_every_change():
    check, production = code(CHECKER), code(PRODUCTION)

    statements = [s.strip().split()[0].upper() for s in check.split(";") if s.strip()]
    assert set(statements) <= {"SELECT", "SET", "PREPARE", "EXECUTE", "DEALLOCATE"}
    for forbidden in ("CREATE ", "ALTER ", "DROP ", "INSERT ", "UPDATE ", "DELETE ", "GRANT "):
        assert forbidden not in check.upper(), forbidden
    listed = re.findall(r"ROW\(\d+, '(\w+)', '([\w.]+)', '(PRESENT|ABSENT)'\)", check)
    names = {name for _, name, _ in listed}
    for trigger_name in re.findall(r"CREATE TRIGGER corporate_intelligence\.(\w+)", production):
        assert trigger_name in names
    assert ("TRIGGER", "trg_gleif_batches_lifecycle", "ABSENT") in listed
    assert ("CHECK", "gleif_import_batches.chk_gleif_batch_counts", "ABSENT") in listed
    assert ("CHECK", "gleif_import_batches.chk_gleif_batch_counts_v2", "PRESENT") in listed
    assert len(listed) == 51


def test_position_registries_make_exclusivity_atomic():
    text = flat(code(PRODUCTION))

    assert "PRIMARY KEY (source_file_id, record_hash_version, source_record_ordinal), " \
           "CONSTRAINT fk_source_position_file" in text
    assert "kind ENUM('OBSERVATION', 'QUARANTINE') NOT NULL" in text
    assert "PRIMARY KEY (batch_id, source_record_ordinal), CONSTRAINT fk_batch_position_batch" in text
    assert "kind ENUM('TRUSTED', 'QUARANTINED', 'DUPLICATE') NOT NULL" in text
    registry = {
        "relationship_observations": ("gleif_source_positions", "OBSERVATION"),
        "legal_entity_observations": ("gleif_source_positions", "OBSERVATION"),
        "gleif_quarantined_records": ("gleif_source_positions", "QUARANTINE"),
        "relationship_batch_observations": ("gleif_batch_positions", "TRUSTED"),
        "entity_batch_observations": ("gleif_batch_positions", "TRUSTED"),
        "gleif_batch_quarantined_records": ("gleif_batch_positions", "QUARANTINED"),
        "gleif_batch_duplicate_occurrences": ("gleif_batch_positions", "DUPLICATE"),
    }
    for table, (target, kind) in registry.items():
        pattern = (rf"AFTER INSERT ON corporate_intelligence\.{table} FOR EACH ROW "
                   rf"INSERT INTO corporate_intelligence\.{target} \([^)]*\) VALUES \([^)]*'{kind}'\)")
        assert re.search(pattern, text), table
    assert len(re.findall(r"AFTER INSERT ON", text)) == 7
    lifecycle = flat(trigger(code(PRODUCTION), "trg_gleif_batches_lifecycle_v2"))
    assert ("SELECT COUNT(*) INTO positions FROM corporate_intelligence.gleif_batch_positions "
            "WHERE batch_id = NEW.batch_id; IF positions <> NEW.records_selected THEN") in lifecycle


def test_every_counter_in_arithmetic_is_not_null_unsigned_and_defaulted():
    """NULL could make a CHECK UNKNOWN (which MySQL treats as passing)."""

    columns = flat(code(APPLIED)) + " " + flat(code(PRODUCTION))
    for counter in ("records_scanned", "records_selected", "observations_inserted",
                    "observations_already_recorded", "identical_duplicates",
                    "quarantined_inserted", "quarantined_already_recorded"):
        assert re.search(rf"\b{counter} BIGINT UNSIGNED NOT NULL DEFAULT 0\b", columns), counter
    # The only nullable term is guarded explicitly.
    assert "(selection_limit IS NULL OR records_scanned <= selection_limit)" in columns
    assert "selection_mode ENUM('ALL_RECORDS', 'LEI_SET', 'FIRST_N') NOT NULL" in columns
