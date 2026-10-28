
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "sql/migrations/m22_3b_report_registry.sql"
TEST = ROOT / "sql/test_report_registry_m22_3b.sql"

DESTRUCTIVE = re.compile(
    r"\b(DROP|TRUNCATE|ALTER|RENAME|GRANT|REVOKE|INSERT|REPLACE\s+INTO"
    r"|DELETE\s+FROM|UPDATE\s+\w+\s+SET)\b",
    re.IGNORECASE,
)


def statements(path):
    """SQL text without comment lines."""
    return "\n".join(
        line for line in path.read_text().splitlines()
        if not line.lstrip().startswith("--")
    )


def tables(sql):
    return sql[sql.index("CREATE TABLE"):sql.index("DELIMITER $$")]


def test_table_definitions_identical():
    production = tables(statements(PRODUCTION))
    test = tables(statements(TEST)).replace(
        "corporate_intelligence_test.", "corporate_intelligence."
    )

    assert production == test


def test_databases_are_never_mixed():
    production, test = statements(PRODUCTION), statements(TEST)

    assert "corporate_intelligence_test" not in production
    assert not re.search(r"corporate_intelligence(?!_test)\b", test)


def test_every_object_name_is_qualified():
    for path, database in (
        (PRODUCTION, "corporate_intelligence"),
        (TEST, "corporate_intelligence_test"),
    ):
        sql = statements(path)
        names = re.findall(
            r"(?:CREATE TABLE IF NOT EXISTS|REFERENCES|CREATE TRIGGER"
            r"|BEFORE (?:UPDATE|DELETE) ON)\s+([\w.]+)",
            sql,
        )
        assert names
        assert all(name.startswith(f"{database}.") for name in names), path


def test_no_destructive_statements():
    for path in (PRODUCTION, TEST):
        assert not DESTRUCTIVE.search(statements(path)), path


def test_registry_is_append_only():
    production, test = statements(PRODUCTION), statements(TEST)

    for table in ("report_runs", "report_run_claims", "report_run_sources"):
        for sql in (production, test):
            assert f"trg_{table}_no_update" in sql
            assert f"trg_{table}_no_delete" in sql

    # Production has no maintenance escape hatch.
    assert "@crie_registry_maintenance" not in production
    assert test.count("@crie_registry_maintenance") == 3
