
import datetime
import decimal
import stat
from unittest.mock import MagicMock

import pytest

from src import e2e_fixture
from src.e2e_fixture import FixtureError, _decode, _encode, require_database, write_fixture


def connection_for(database, counts=()):
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.fetchone.side_effect = [(database,), *[(n,) for n in counts]]
    return connection, cursor


@pytest.mark.parametrize("call, expected", [
    (lambda c: e2e_fixture.extract(c, 1), "corporate_intelligence"),
    (lambda c: e2e_fixture.load(c, {"tables": {}}), "corporate_intelligence_e2e"),
    (lambda c: e2e_fixture.remove(c, {"tables": {}}), "corporate_intelligence_e2e"),
])
def test_every_operation_checks_its_database(call, expected):
    for wrong in ("corporate_intelligence_test", "mysql",
                  "corporate_intelligence_e2e" if expected == "corporate_intelligence"
                  else "corporate_intelligence"):
        connection, _ = connection_for(wrong)
        with pytest.raises(FixtureError, match=f"expected {expected!r}"):
            call(connection)
        connection.commit.assert_not_called()


def test_load_refuses_existing_identifiers_and_rolls_back():
    fixture = {"tables": {table: [] for table, _, _ in e2e_fixture.TABLES}}
    fixture["tables"]["investigation_cases"] = [{"case_id": 1}]
    connection, cursor = connection_for("corporate_intelligence_e2e", counts=[1])

    with pytest.raises(FixtureError, match="already holds"):
        e2e_fixture.load(connection, fixture)

    connection.commit.assert_not_called()
    assert not any("INSERT" in c.args[0] for c in cursor.execute.call_args_list)


def test_values_round_trip():
    for value in (datetime.datetime(2026, 1, 2, 3, 4, 5, 6), datetime.date(2025, 9, 30),
                  decimal.Decimal("49.50"), b"\x00\x01", "text", 7, None):
        assert _decode(_encode(value)) == value


def test_fixture_written_once_and_privately(tmp_path):
    path = tmp_path / "private" / "case.json"
    write_fixture({"fixture_version": 1}, path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with pytest.raises(FileExistsError):
        write_fixture({"fixture_version": 1}, path)


def test_cli_refuses_tracked_locations_in_repository():
    assert e2e_fixture.main(["extract", "--case-id", "1",
                             "--output", str(e2e_fixture.ROOT / "tests" / "case.json")]) == 2


def test_require_database_accepts_exact_name():
    connection, _ = connection_for("corporate_intelligence_e2e")
    require_database(connection, "corporate_intelligence_e2e")
