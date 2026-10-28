
"""
Canonical records survive MySQL JSON (23B.2B-3a): a canonical record stored
as JSON and read back reproduces its record hash when re-serialized with
gleif_observations.canonical_bytes. Read-only SELECTs on the test database;
nothing is created or written.

MySQL does not keep the stored text: it re-orders object keys (shortest
first) and re-spaces the document. So the hash is never computed from the
JSON text MySQL returns, only from canonical_bytes(json.loads(text)), which
is exact because canonical records contain only strings, integers,
booleans, null, arrays (whose order MySQL keeps) and objects.
"""

import json

import pytest

from src.gleif_observations import canonical_bytes, record_sha256

from test_mysql_evidence_snapshot import block_default_connections, connect, pytestmark  # noqa: F401
from test_gleif_lei2 import RICH, SPARSE, parse_records, record

EDGE = {
    "nfc": "Café", "nfd": "Café", "emoji": "Synthetic \U0001F3E2 plc",
    "quotes": 'He said "x" \\ back\\slash', "control": "line\nbreak\ttab\u0001",
    "markup": "</lei:LegalName> & <x>", "separator": "  ",
    "big": 999_999_999, "zero": 0, "negative": -1, "flag": True, "none": None,
    "empty_list": [], "empty_object": {}, "nested": [[3, 2, 1], {"b": [None, "a"], "a": 1}],
}


def through_mysql(value):
    connection = connect()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT CAST(%s AS JSON)", (canonical_bytes(value).decode("utf-8"),))
        text = cursor.fetchone()[0]
    finally:
        cursor.close()
        connection.rollback()
        connection.close()
    return text if isinstance(text, str) else text.decode("utf-8")


@pytest.fixture
def lei2_fields(tmp_path):
    _, records = parse_records(tmp_path, [record(**RICH), record(**SPARSE)])
    return [r for r in records]


def test_lei2_canonical_records_round_trip_to_the_same_hash(lei2_fields):
    for parsed in lei2_fields:
        text = through_mysql(parsed["fields"])
        assert text != canonical_bytes(parsed["fields"]).decode()    # MySQL re-formats...
        assert record_sha256(json.loads(text)) == parsed["record_sha256"]   # ...hash survives
        events = json.loads(text)["legal_entity_events"]
        if events:                                                   # array order kept
            assert [e["type"] for e in events] == [e["type"] for e in
                                                   parsed["fields"]["legal_entity_events"]]


def test_edge_values_round_trip_exactly():
    back = json.loads(through_mysql(EDGE))

    assert back == EDGE
    assert canonical_bytes(back) == canonical_bytes(EDGE)
    assert back["nfc"] != back["nfd"]                                # no Unicode normalization


def test_rr_canonical_records_round_trip(tmp_path):
    from gleif_rr_samples import relationship, synthetic_lei, write
    from src.gleif_rr import RecordStream, scan_file

    path = write(tmp_path / "rr.xml", [relationship(synthetic_lei("SYNTHJSA"),
                                                    synthetic_lei("SYNTHJSB"), status="NULL")])
    scan = scan_file(path)
    with RecordStream(path, scan) as stream:
        (parsed,) = list(stream)
    assert record_sha256(json.loads(through_mysql(parsed["fields"]))) == parsed["record_sha256"]
