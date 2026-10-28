
"""
The end-to-end database scripts (sql/e2e) can only affect
corporate_intelligence_e2e, and mirror their sources exactly.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
E2E = ROOT / "sql" / "e2e"
FILES = sorted(E2E.glob("m23a_e2e_0*.sql"))
GRANT = E2E / "m23a_e2e_05_grant.sql"
# The deployment package leaves out the local-account grant (deploy/exclude.txt).
NEEDS_GRANT = pytest.mark.skipif(not GRANT.exists(),
                                 reason="local-account grant script not in this copy")


def code(path):
    return "\n".join(line for line in path.read_text().splitlines()
                     if not line.lstrip().startswith("--"))


@NEEDS_GRANT
def test_five_scripts_in_order():
    assert [p.name for p in FILES] == [
        "m23a_e2e_01_create.sql", "m23a_e2e_02_schema.sql",
        "m23a_e2e_03_registry.sql", "m23a_e2e_04_lineage.sql",
        "m23a_e2e_05_grant.sql",
    ]


def test_only_the_e2e_database_is_named():
    for path in FILES:
        names = set(re.findall(r"corporate_intelligence\w*", code(path)))
        assert names == {"corporate_intelligence_e2e"}, (path.name, names)


def test_no_destructive_statements():
    for path in FILES:
        text = code(path)
        assert not re.search(r"\bDROP\s+(DATABASE|TABLE|SCHEMA)\b", text, re.I), path.name
        assert not re.search(r"\b(TRUNCATE|DELETE\s+FROM|INSERT\s+INTO|UPDATE\s+`?\w+`?\s+SET)\b",
                             text, re.I), path.name
        # The only DROP is the view stand-in that mysqldump writes.
        assert re.findall(r"\bDROP\b.*", text) in (
            [], ["DROP VIEW IF EXISTS `v_current_disclosure_claims`*/;"] * 2,
            ["DROP VIEW IF EXISTS `v_current_disclosure_claims`*/;"],
        ), path.name


def test_create_fails_if_database_exists():
    text = code(FILES[0])
    assert "CREATE DATABASE `corporate_intelligence_e2e`" in text
    assert "IF NOT EXISTS" not in text


def test_schema_selects_e2e_before_any_definition():
    text = code(FILES[1])
    use = text.index("USE `corporate_intelligence_e2e`;")
    first_create = min(text.index("CREATE TABLE"), text.index("CREATE VIEW"))
    assert use < first_create
    # Production tables and the current-claims view, unqualified.
    for table in ("investigation_cases", "case_sources", "disclosure_claims",
                  "claim_corrections", "legal_entities", "corporate_relationships",
                  "relationship_evidence", "relationship_periods"):
        assert f"CREATE TABLE `{table}`" in text
    assert "VIEW `v_current_disclosure_claims` AS" in text


def test_registry_and_lineage_equal_their_test_twins():
    for path, twin in ((FILES[2], "sql/test_report_registry_m22_3b.sql"),
                       (FILES[3], "sql/test_schema_upgrade_m22_4a.sql")):
        source = code(ROOT / twin).replace("corporate_intelligence_test", "corporate_intelligence_e2e")
        assert code(path).strip() == source.strip(), path.name
