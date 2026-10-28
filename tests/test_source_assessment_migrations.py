
"""The 23B.1 migration and its test and E2E twins (not executed here)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "sql/migrations/m23b1_source_relationship_assessments.sql"
TWINS = {
    "corporate_intelligence_test": ROOT / "sql/test_schema_upgrade_m23b1.sql",
    "corporate_intelligence_e2e": ROOT / "sql/e2e/m23b1_e2e_source_assessments.sql",
}
FLAGGED_DELETE = re.compile(
    r"BEGIN\s+IF COALESCE\(@crie_assessment_maintenance, 0\) <> 1 THEN\s+"
    r"SIGNAL SQLSTATE '45000'\s+SET MESSAGE_TEXT = "
    r"'source_relationship_assessments is append-only';\s+END IF;\s+END\$\$")


def code(path):
    return "\n".join(line for line in path.read_text().splitlines()
                     if not line.lstrip().startswith("--")).strip()


def test_production_migration_is_additive_qualified_and_strict():
    text = code(PRODUCTION)

    assert set(re.findall(r"corporate_intelligence\w*", text)) == {"corporate_intelligence"}
    names = re.findall(r"\b(?:TABLE|REFERENCES|ON|TRIGGER)\s+([A-Za-z_][\w.]*)", text)
    assert names and all(n.startswith("corporate_intelligence.") for n in names), names
    for forbidden in ("DROP ", "DELETE FROM", "INSERT INTO", "GRANT ", "USE ", "maintenance",
                      "UPDATE corporate"):
        assert forbidden not in text, forbidden
    assert text.count("CREATE TRIGGER") == 3
    # Production deletion is unconditional.
    assert "BEFORE DELETE ON corporate_intelligence.source_relationship_assessments FOR EACH ROW\n" \
           "SIGNAL SQLSTATE '45000'" in text


def test_required_constraints_present():
    text = code(PRODUCTION)
    for name in ("uq_case_source", "uq_assessment_submission", "uq_assessment_successor",
                 "uq_assessment_root", "fk_assessment_source_a", "fk_assessment_source_b",
                 "fk_assessment_evidence_source", "fk_assessment_supersedes",
                 "chk_assessment_pair_order", "chk_assessment_scope_complete",
                 "chk_assessment_independence_evidence",
                 "trg_source_assessments_supersession",
                 "trg_source_assessments_no_update", "trg_source_assessments_no_delete"):
        assert name in text, name
    assert "FOREIGN KEY (source_a_id, case_id)" in text
    assert "REFERENCES corporate_intelligence.case_sources (source_id, case_id)" in text


def test_twins_differ_only_by_database_and_flagged_delete():
    production = code(PRODUCTION)
    for database, path in TWINS.items():
        twin = code(path)
        assert set(re.findall(r"corporate_intelligence\w*", twin)) == {database}
        assert len(FLAGGED_DELETE.findall(twin)) == 1
        restored = FLAGGED_DELETE.sub(
            "SIGNAL SQLSTATE '45000'\n    SET MESSAGE_TEXT = "
            "'source_relationship_assessments is append-only'$$", twin)
        assert restored.replace(database, "corporate_intelligence") == production


TRIGGER = re.compile(r"CREATE TRIGGER (\S+)\.trg_source_assessments_supersession.*?END\$\$", re.S)
FIXES = {
    "corporate_intelligence_test": ROOT / "sql/test_fix_m23b1_supersession_trigger.sql",
    "corporate_intelligence_e2e": ROOT / "sql/e2e/m23b1_e2e_fix_supersession_trigger.sql",
}


def test_supersession_trigger_compares_scope_text_exactly():
    for path in (PRODUCTION, *TWINS.values(), *FIXES.values()):
        trigger = TRIGGER.search(code(path)).group(0)
        assert "(p.entity_name COLLATE utf8mb4_0900_bin) <=> (NEW.entity_name COLLATE utf8mb4_0900_bin)" in trigger
        assert "(p.metric COLLATE utf8mb4_0900_bin) <=> (NEW.metric COLLATE utf8mb4_0900_bin)" in trigger
        assert "p.entity_name <=> NEW.entity_name" not in trigger


def test_trigger_fixes_replace_only_that_trigger_with_the_migration_definition():
    for database, path in FIXES.items():
        text = code(path)
        assert set(re.findall(r"corporate_intelligence\w*", text)) == {database}
        assert re.findall(r"\b(DROP|CREATE|ALTER|INSERT|UPDATE|DELETE|GRANT)\b(?! ON)", text) == [
            "DROP", "CREATE"]
        assert f"DROP TRIGGER {database}.trg_source_assessments_supersession;" in text
        assert TRIGGER.search(text).group(0) == TRIGGER.search(code(TWINS[database])).group(0)
