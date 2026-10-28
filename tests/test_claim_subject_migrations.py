
"""The 23B.2A migration and its test and E2E twins (not executed here)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "sql/migrations/m23b2a_claim_subject_mappings.sql"
TWINS = {
    "corporate_intelligence_test": ROOT / "sql/test_schema_upgrade_m23b2a.sql",
    "corporate_intelligence_e2e": ROOT / "sql/e2e/m23b2a_e2e_claim_subject_mappings.sql",
}
FLAGGED_DELETE = re.compile(
    r"BEGIN\s+IF COALESCE\(@crie_mapping_maintenance, 0\) <> 1 THEN\s+"
    r"SIGNAL SQLSTATE '45000'\s+SET MESSAGE_TEXT = "
    r"'claim_subject_mappings is append-only';\s+END IF;\s+END\$\$")
LEGAL_ENTITIES = re.compile(
    r"CREATE TABLE IF NOT EXISTS corporate_intelligence_test\.legal_entities \(.*?\) ENGINE=InnoDB"
    r"[^;]*;\s*", re.S)


def code(path):
    return "\n".join(line for line in path.read_text().splitlines()
                     if not line.lstrip().startswith("--")).strip()


def test_production_migration_is_additive_qualified_and_strict():
    text = code(PRODUCTION)

    assert set(re.findall(r"corporate_intelligence\w*", text)) == {"corporate_intelligence"}
    names = re.findall(r"\b(?:TABLE|REFERENCES|ON|TRIGGER)\s+([A-Za-z_][\w.]*)", text)
    assert names and all(n.startswith("corporate_intelligence.") for n in names), names
    for forbidden in ("DROP ", "DELETE FROM", "INSERT INTO", "ALTER ", "GRANT ", "USE ",
                      "maintenance", "UPDATE corporate"):
        assert forbidden not in text, forbidden
    assert "BEFORE DELETE ON corporate_intelligence.claim_subject_mappings FOR EACH ROW\n" \
           "SIGNAL SQLSTATE '45000'" in text


def test_exact_subject_semantics_and_constraints():
    text = code(PRODUCTION)

    assert "subject_text VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL" in text
    assert "(p.subject_text COLLATE utf8mb4_0900_bin) = (NEW.subject_text COLLATE utf8mb4_0900_bin)" in text
    # The LEI column matches legal_entities.lei for the foreign key.
    assert "lei CHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL" in text
    for name in ("uq_subject_mapping_submission", "uq_subject_mapping_successor",
                 "uq_subject_mapping_root", "fk_subject_mapping_case", "fk_subject_mapping_lei",
                 "fk_subject_mapping_evidence_source", "fk_subject_mapping_supersedes",
                 "chk_subject_mapping_status_shape", "chk_subject_mapping_evidence",
                 "chk_subject_mapping_limitations", "trg_subject_mappings_supersession",
                 "trg_subject_mappings_no_update", "trg_subject_mappings_no_delete"):
        assert name in text, name
    assert "FOREIGN KEY (evidence_source_id, case_id)" in text


def test_twins_differ_only_by_database_flagged_delete_and_test_legal_entities():
    production = code(PRODUCTION)
    for database, path in TWINS.items():
        twin = code(path)
        assert set(re.findall(r"corporate_intelligence\w*", twin)) == {database}
        assert len(FLAGGED_DELETE.findall(twin)) == 1
        twin = FLAGGED_DELETE.sub(
            "SIGNAL SQLSTATE '45000'\n    SET MESSAGE_TEXT = "
            "'claim_subject_mappings is append-only'$$", twin)
        if database == "corporate_intelligence_test":
            assert len(LEGAL_ENTITIES.findall(twin)) == 1
            twin = LEGAL_ENTITIES.sub("", twin)
        else:
            assert "legal_entities (" not in twin.split("CREATE TABLE")[0]
        assert twin.replace(database, "corporate_intelligence") == production


def test_test_legal_entities_matches_production_definition():
    dump_columns = ["`lei` char(20)", "`legal_name` varchar(1000)", "`jurisdiction` varchar(20)",
                    "`country` varchar(10)", "`registration_status` varchar(50)",
                    "`created_at` timestamp", "PRIMARY KEY (`lei`)"]
    block = LEGAL_ENTITIES.search(code(TWINS["corporate_intelligence_test"])).group(0)
    assert all(column in block for column in dump_columns)
    assert "COLLATE=utf8mb4_unicode_ci" in block
