
"""Safe streaming RR-CDF parsing, validation and canonical hashing (23B.2B-2). No database."""

import os
import tracemalloc
from pathlib import Path

import pytest

from src import gleif_rr
from src.gleif_observations import canonical_bytes, record_sha256
from src.gleif_rr import RecordStream, RRFormatError, SourceChangedError, scan_file

from gleif_rr_samples import document, relationship, synthetic_lei, write

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gleif"
SUPPORTED = FIXTURES / "rr_cdf_1_1_synthetic.xml"
UNSUPPORTED = FIXTURES / "rr_unsupported_namespace_synthetic.xml"
A, B, C = synthetic_lei("SYNTHRRA"), synthetic_lei("SYNTHRRB"), synthetic_lei("SYNTHRRC")

# Canonical record-hash version 1, pinned: changing the canonical form or
# the hashing must be a deliberate new version, never a silent change.
FIXTURE_SHA256 = "5b160372effa6eefc34f145789ea3c4a15ededd4ad429957c87a49655937da87"
RECORD_1_SHA256 = "51ca66ca92ac13643e1e3dc98286ab3203979301de09e16b99993a519bce9834"
RECORD_2_SHA256 = "943fb9d57692e75363db763b44ce7520e3a4bbe47a89943a7a106775ad0c975d"


def records(path):
    scan = scan_file(path)
    with RecordStream(path, scan) as stream:
        found = list(stream)
        stream.verify()
    return scan, found


def test_supported_fixture_header_records_and_unstated_values():
    scan, (first, second) = records(SUPPORTED)

    assert scan["sha256"] == FIXTURE_SHA256 and scan["record_count"] == 2
    assert scan["header"]["publication_scope"] == "FULL"
    assert scan["header"]["content_date_text"] == "2026-10-22T09:00:01.250Z"
    assert scan["header"]["content_date_utc"].microsecond == 250000
    assert first["fields"]["validation_sources"] == "ENTITY_SUPPLIED_ONLY"
    assert [p["period_type"] for p in first["fields"]["periods"]] == ["ACCOUNTING_PERIOD",
                                                                      "RELATIONSHIP_PERIOD"]
    assert first["fields"]["periods"][1]["end_text"] is None       # stated open, not "ended"
    # Unstated values stay None; nothing is filled in.
    for field in ("validation_sources", "validation_documents", "validation_reference",
                  "periods", "qualifiers", "quantifiers"):
        assert second["fields"][field] is None, field
    assert (second["fields"]["relationship_status"],
            second["fields"]["registration_status"]) == ("INACTIVE", "RETIRED")


def test_canonical_hash_version_1_fixed_values():
    _, (first, second) = records(SUPPORTED)

    assert (first["record_sha256"], second["record_sha256"]) == (RECORD_1_SHA256, RECORD_2_SHA256)
    assert first["record_hash_version"] == 1
    assert record_sha256(first["fields"]) == RECORD_1_SHA256
    assert canonical_bytes({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode()


def test_whitespace_and_root_attributes_do_not_change_record_hashes(tmp_path):
    base = relationship(A, B, reference="Annual report 2023, note 4")
    spaced = (base.replace("><", ">\n    <")
              .replace("<rr:ValidationReference>", "<rr:ValidationReference>\n  ")
              .replace("</rr:ValidationReference>", "  \n</rr:ValidationReference>"))
    one = write(tmp_path / "one.xml", [base])
    two = tmp_path / "two.xml"
    two.write_text(document([spaced]).replace(
        '<rr:RelationshipData xmlns:rr="http://www.gleif.org/data/schema/rr/2016">',
        '<rr:RelationshipData xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:rr="http://www.gleif.org/data/schema/rr/2016">'))

    (_, [r1]), (_, [r2]) = records(one), records(two)
    assert r1["record_sha256"] == r2["record_sha256"]
    assert r2["fields"]["validation_reference"] == "Annual report 2023, note 4"


@pytest.mark.parametrize("change", [
    {"status": "INACTIVE"},
    {"registration": "LAPSED"},
    {"validation": "FULLY_CORROBORATED"},
    {"validation": None},
    {"reference": "note 5"},
    {"periods": (("RELATIONSHIP_PERIOD", "2019-05-01T00:00:00Z", None),
                 ("ACCOUNTING_PERIOD", "2023-01-01T00:00:00Z", "2023-12-31T00:00:00Z"))},
    {"periods": (("ACCOUNTING_PERIOD", "2023-01-01T00:00:00+00:00", "2023-12-31T00:00:00Z"),
                 ("RELATIONSHIP_PERIOD", "2019-05-01T00:00:00Z", None))},
    {"qualifiers": (("ACCOUNTING_STANDARD", "US_GAAP"),)},
    {"qualifiers": ()},
    {"quantifiers": (("ACCOUNTING_CONSOLIDATION", "100.0", "PERCENTAGE"),)},
    {"last_update": "2026-01-15T10:00:00.001Z"},
])
def test_material_differences_change_the_hash(tmp_path, change):
    (_, [base]), (_, [other]) = (records(write(tmp_path / "a.xml", [relationship(A, B)])),
                                 records(write(tmp_path / "b.xml", [relationship(A, B, **change)])))
    assert base["record_sha256"] != other["record_sha256"]


def test_unsupported_namespace_is_rejected():
    with pytest.raises(RRFormatError, match="Unsupported RR namespace"):
        scan_file(UNSUPPORTED)


@pytest.mark.parametrize("prolog", [
    '<!DOCTYPE lol [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>\n',
    '<!DOCTYPE x [<!ENTITY ext SYSTEM "file:///etc/passwd">]>\n',
    '<!DOCTYPE x SYSTEM "http://example.invalid/rr.dtd">\n',
])
def test_dtds_and_entities_are_refused_before_expansion(tmp_path, prolog):
    path = write(tmp_path / "unsafe.xml", [relationship(A, B)], prolog=prolog)
    with pytest.raises(RRFormatError, match="DTDs and entity declarations"):
        scan_file(path)


@pytest.mark.parametrize("mutate, message", [
    (lambda x: x.replace("</rr:Registration>", "</rr:Registratio>"), "Malformed XML"),
    (lambda x: x.replace("<rr:Registration>", "<rr:Registration>stray text"), "Unexpected text"),
    (lambda x: x.replace("</rr:Registration>", "</rr:Registration><rr:Extension/>"),
     "Valid GLEIF structure not supported: Extension in RelationshipRecord"),
    (lambda x: x.replace("<rr:RelationshipRecord>", '<rr:RelationshipRecord id="1">'),
     "Unexpected attributes"),
    (lambda x: x.replace("<rr:RelationshipStatus>ACTIVE</rr:RelationshipStatus>", ""),
     "needs 1..1 RelationshipStatus"),
    (lambda x: x.replace("<rr:ManagingLOU>", "<rr:ManagingLOU></rr:ManagingLOU><rr:ManagingLOU>"),
     "is empty|needs"),
    (lambda x: x.replace('xmlns:rr="http://www.gleif.org/data/schema/rr/2016">',
                         'xmlns:rr="http://www.gleif.org/data/schema/rr/2016" '
                         'xmlns:o="urn:other"><o:Other/>'), "outside the RR namespace"),
])
def test_malformed_or_unsupported_structure_is_rejected(tmp_path, mutate, message):
    path = tmp_path / "bad.xml"
    path.write_text(mutate(document([relationship(A, B)])))
    with pytest.raises(RRFormatError, match=message):
        scan_file(path)


@pytest.mark.parametrize("kwargs, message", [
    ({"child": "NOT-AN-LEI"}, "not a valid LEI"),
    ({"child_type": "BIC"}, "must identify an LEI"),
    ({"child": B}, "to itself"),
    ({"status": "ENDED"}, "Unsupported RelationshipStatus"),
    ({"kind": "IS_OWNED_BY"}, "Unsupported RelationshipType"),
    ({"registration": "ACTIVE"}, "Unsupported RegistrationStatus"),
    ({"validation": "CONFIRMED"}, "Unsupported ValidationSources"),
    ({"periods": (("ACCOUNTING_PERIOD", "2023-12-31T00:00:00Z", "2023-01-01T00:00:00Z"),)},
     "ends before it starts"),
    ({"periods": (("ACCOUNTING_PERIOD", "2023-01-01", None),)}, "time zone"),
    ({"periods": (("ACCOUNTING_PERIOD", "2023-01-01T00:00:00.0001Z", None),)}, "milliseconds"),
    ({"periods": (("FISCAL_YEAR", "2023-01-01T00:00:00Z", None),)}, "Unsupported PeriodType"),
    ({"quantifiers": (("ACCOUNTING_CONSOLIDATION", "NaN", None),)}, "not a decimal"),
    ({"quantifiers": (("ACCOUNTING_CONSOLIDATION", "1E5", None),)}, "not a decimal"),
    ({"child_type": "ISO_17442_COMPATIBLE", "child": "NOT-AN-ID"}, "not a valid LEI"),
    ({"qualifiers": (("ACCOUNTING_STANDARD", "ifrs lower"),)}, "QualifierCategory"),
])
def test_invalid_values_are_rejected(tmp_path, kwargs, message):
    child = kwargs.pop("child", A)
    path = write(tmp_path / "bad.xml", [relationship(child, B, **kwargs)])
    with pytest.raises(RRFormatError, match=message):
        scan_file(path)


@pytest.mark.parametrize("header, message", [
    ({"record_count": 2}, "RecordCount says 2"),
    ({"record_count": "two"}, "not a whole number"),
    ({"file_content": "SOMETHING_ELSE"}, "Unsupported FileContent"),
    ({"file_content": "GLEIF_DELTA_PUBLISHED"}, "DeltaStart is required"),
    ({"delta_start": "2026-10-21T09:00:00Z"}, "DeltaStart is required"),
    ({"content_date": "22/10/2026"}, "ContentDate"),
])
def test_malformed_headers_are_rejected(tmp_path, header, message):
    path = write(tmp_path / "bad.xml", [relationship(A, B)], **header)
    with pytest.raises(RRFormatError, match=message):
        scan_file(path)


def test_delta_header_is_recorded(tmp_path):
    path = write(tmp_path / "delta.xml", [relationship(A, B)],
                 file_content="GLEIF_DELTA_PUBLISHED", delta_start="2026-10-21T09:00:00Z")
    header = scan_file(path)["header"]
    assert (header["publication_scope"], header["delta_basis"]) == ("DELTA", "2026-10-21T09:00:00Z")


def test_parsing_is_streaming_with_bounded_memory(tmp_path):
    def peak(count):
        path = write(tmp_path / f"many-{count}.xml",
                     [relationship(A, B, reference=f"Ref {i:06d}") for i in range(count)])
        tracemalloc.start()
        scan = scan_file(path)
        with RecordStream(path, scan) as stream:
            assert sum(1 for _ in stream) == count
            stream.verify()
        size = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        return os.path.getsize(path), size

    small_bytes, small_peak = peak(400)
    large_bytes, large_peak = peak(4_000)
    assert large_bytes > 9 * small_bytes
    # Ten times the data, roughly the same memory: bounded by the read
    # block, not by the file.
    assert large_peak < 1.5 * small_peak + (1 << 20)
    assert large_peak < 16 << 20


def test_file_changed_after_registration_is_detected(tmp_path):
    path = write(tmp_path / "f.xml", [relationship(A, B), relationship(C, B)])
    scan = scan_file(path)
    path.write_text(document([relationship(A, B), relationship(C, B, status="INACTIVE")]))
    with pytest.raises(SourceChangedError):
        RecordStream(path, scan)


def test_same_size_and_mtime_change_is_caught_by_the_hash(tmp_path):
    path = write(tmp_path / "f.xml", [relationship(A, B, status="ACTIVE")])
    scan = scan_file(path)
    stat = os.stat(path)
    path.write_text(path.read_text().replace(">ACTIVE<", ">ACTIVA<"))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    stream = RecordStream(path, scan)          # identity looks the same...
    with stream, pytest.raises((SourceChangedError, RRFormatError)):
        list(stream)                           # ...but the content does not
        stream.verify()


def test_first_n_stops_parsing_but_still_hashes_the_rest(tmp_path, monkeypatch):
    path = write(tmp_path / "f.xml", [relationship(A, B, reference=str(i)) for i in range(50)])
    scan = scan_file(path)
    built = []
    real = gleif_rr.make_record
    monkeypatch.setattr(gleif_rr, "make_record", lambda o, n: built.append(o) or real(o, n))

    with RecordStream(path, scan) as stream:
        for record in stream:
            if record["ordinal"] == 3:
                break
        stream.verify()
    assert built == [1, 2, 3]

    path.write_text(path.read_text().replace("<rr:ValidationReference>49<",
                                             "<rr:ValidationReference>4X<"))
    with RecordStream(path, {**scan, "identity": gleif_rr._identity(path)}) as stream:
        for record in stream:
            if record["ordinal"] == 3:
                break
        with pytest.raises(SourceChangedError, match="SHA-256"):
            stream.verify()


def test_each_record_is_checked_against_the_first_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(gleif_rr, "BLOCK_BYTES", 256)          # read a little at a time
    path = write(tmp_path / "f.xml", [relationship(A, B, reference=f"Ref {i}") for i in range(5)])
    scan = scan_file(path)
    stat = os.stat(path)

    seen = []
    with RecordStream(path, scan) as stream, pytest.raises(SourceChangedError, match="Record 5 differs"):
        for record in stream:
            seen.append(record["ordinal"])
            if record["ordinal"] == 1:      # changed after it was registered, identity kept
                path.write_text(path.read_text().replace("Ref 4", "Ref X"))
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert seen == [1, 2, 3, 4]             # the changed record is never handed out


def test_tracked_gleif_fixtures_are_small_and_synthetic():
    # Every file in the directory, whatever its case: the ignore exception
    # admits *_synthetic.xml, so anything else appearing here fails.
    files = sorted(FIXTURES.iterdir())
    assert [f.name for f in files] == ["rr_cdf_1_1_synthetic.xml",
                                       "rr_unsupported_namespace_synthetic.xml"]
    for path in files:
        assert path.stat().st_size < 16_384
        assert "SYNTH" in path.read_text()
