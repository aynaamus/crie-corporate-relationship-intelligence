
"""GLEIF RR importer rules that need no database (23B.2B-2)."""

import pytest

from src import gleif_import
from src.gleif_import import ImportRefused, import_rr_file, matched_endpoint, selects
from src.gleif_observations import canonical_selection

from gleif_rr_samples import relationship, synthetic_lei, write

A, B, C = synthetic_lei("SYNTHIMA"), synthetic_lei("SYNTHIMB"), synthetic_lei("SYNTHIMC")
KEY = (A, B, "IS_DIRECTLY_CONSOLIDATED_BY")


def test_selection_semantics():
    every = canonical_selection("RR", "ALL_RECORDS")
    first = canonical_selection("RR", "FIRST_N", limit=2)
    child = canonical_selection("RR", "LEI_SET", leis=[A], match="CHILD")
    parent = canonical_selection("RR", "LEI_SET", leis=[A], match="PARENT")
    either = canonical_selection("RR", "LEI_SET", leis=[A, B], match="CHILD_OR_PARENT")

    assert selects(every, 99, KEY)
    assert selects(first, 2, KEY) and not selects(first, 3, KEY)
    assert selects(child, 1, KEY) and not selects(parent, 1, KEY)
    assert selects(parent, 1, (B, A, KEY[2])) and not selects(child, 1, (B, C, KEY[2]))
    assert matched_endpoint(child, KEY) == "CHILD"
    assert matched_endpoint(parent, (C, A, KEY[2])) == "PARENT"
    assert matched_endpoint(either, KEY) == "BOTH"
    assert matched_endpoint(every, KEY) is None


class FakeCursor:
    def __init__(self, log, database):
        self.log, self.database = log, database

    def execute(self, sql, params=()):
        self.log.append(sql)

    def fetchone(self):
        return (self.database,)

    def close(self):
        pass


class FakeConnection:
    def __init__(self, database):
        self.database, self.log, self.closed = database, [], False

    def cursor(self, **_):
        return FakeCursor(self.log, self.database)

    def rollback(self):
        pass

    def close(self):
        self.closed = True


@pytest.mark.parametrize("database", ["corporate_intelligence", "mysql", None])
def test_any_other_database_is_refused_before_writing(tmp_path, database):
    path = write(tmp_path / "f.xml", [relationship(A, B)])
    connection = FakeConnection(database)

    with pytest.raises(ImportRefused):
        import_rr_file(lambda: connection, path, canonical_selection("RR", "ALL_RECORDS"),
                       started_by="synthetic test")
    assert connection.log[0] == "SELECT DATABASE()"
    assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                   for s in connection.log)
    assert connection.closed


def test_malformed_file_is_rejected_before_connecting(tmp_path):
    path = write(tmp_path / "f.xml", [relationship(A, B)], record_count=5)
    with pytest.raises(ValueError, match="RecordCount"):
        import_rr_file(lambda: pytest.fail("connected"), path,
                       canonical_selection("RR", "ALL_RECORDS"), started_by="synthetic test")


@pytest.mark.parametrize("kwargs", [{"lease_seconds": 0}, {"lease_seconds": 7200},
                                    {"chunk_size": 0}])
def test_invalid_import_parameters(tmp_path, kwargs):
    path = write(tmp_path / "f.xml", [relationship(A, B)])
    with pytest.raises(ValueError):
        import_rr_file(lambda: pytest.fail("connected"), path,
                       canonical_selection("RR", "ALL_RECORDS"), started_by="t", **kwargs)


def test_lei2_selections_are_not_imported_here(tmp_path):
    path = write(tmp_path / "f.xml", [relationship(A, B)])
    with pytest.raises(ValueError, match="Only RR"):
        import_rr_file(lambda: pytest.fail("connected"), path,
                       canonical_selection("LEI2", "ALL_RECORDS"), started_by="t")


def test_cli_accepts_only_isolated_databases(tmp_path):
    path = write(tmp_path / "f.xml", [relationship(A, B)])
    with pytest.raises(SystemExit):
        gleif_import.main([str(path), "--database", "corporate_intelligence",
                           "--mode", "ALL_RECORDS", "--started-by", "t"])
    with pytest.raises(ImportRefused):
        gleif_import._connect_to("corporate_intelligence")
