
"""LEI-CDF (LEI2) parsing, validation and canonical hashing (23B.2B-3a). No database."""

import os
import tracemalloc

import pytest

from src import gleif_lei2
from src.gleif_lei2 import FLAG_INVALID_CHECK_DIGITS, LEI2FormatError, RecordStream, scan_file
from src.gleif_xml import CONCATENATED_HEADER_NS, SourceChangedError

from gleif_lei2_samples import document, event, record, write
from gleif_rr_samples import LOU, synthetic_lei
from gleif_rr_samples import document as rr_document, relationship

A, B, C = synthetic_lei("SYNTHLEIA"), synthetic_lei("SYNTHLEIB"), synthetic_lei("SYNTHLEIC")
BAD = A[:-2] + f"{(int(A[-2:]) + 1) % 100:02d}"
RICH = dict(
    other_names=(("Synthetic Alte AG", "PREVIOUS_LEGAL_NAME", "de"),
                 ("Synth Trade", "TRADING_OR_OPERATING_NAME", "en")),
    transliterated=(("Synthetic Holdings AG", "PREFERRED_ASCII_TRANSLITERATED_LEGAL_NAME", "de"),),
    successors=(B, "name:Synthetic Nachfolger AG"),
    events=(event(affected=(("/LEIRecord/Entity/LegalName", "Synthetic Alte AG"),)),
            event(kind="LIQUIDATION", status="IN_PROGRESS", effective=None)))
SPARSE = dict(lei=C, status="NULL", category=None, legal_form=None, creation=None,
              validation=None, validation_authority=False, authority=False, jurisdiction=None)
# Canonical LEI2 record-hash version 1, pinned.
RICH_SHA256 = "0b9558928b7a1a6f91705fc95870175c7cefdeff8c11cc6d82bf130973ddc296"
SPARSE_SHA256 = "dd4b6a5fb23a7661c6a580fe40a16393ac5780e5ffbba23006b96a49c1427f57"


def parse(path):
    scan = scan_file(path)
    with RecordStream(path, scan) as stream:
        found = list(stream)
        stream.verify()
    return scan, found


def parse_records(tmp_path, records, name="f.xml", **kwargs):
    return parse(write(tmp_path / name, records, **kwargs))


def one(tmp_path, **kwargs):
    return parse_records(tmp_path, [record(**kwargs)])[1][0]


def test_rich_record_preserves_names_events_successors_and_dates(tmp_path):
    fields = one(tmp_path, **RICH)["fields"]

    assert fields["legal_name"] == {"text": "Synthetic Holdings AG", "lang": "de"}
    assert [(n["type"], n["lang"], n["text"]) for n in fields["other_entity_names"]] == [
        ("PREVIOUS_LEGAL_NAME", "de", "Synthetic Alte AG"),
        ("TRADING_OR_OPERATING_NAME", "en", "Synth Trade")]
    assert fields["transliterated_other_entity_names"][0]["type"] == \
        "PREFERRED_ASCII_TRANSLITERATED_LEGAL_NAME"
    assert fields["successor_entities"] == [
        {"lei": B, "name": None},
        {"lei": None, "name": {"text": "Synthetic Nachfolger AG", "lang": "de"}}]
    first, second = fields["legal_entity_events"]
    # Effective and recorded dates stay apart, each verbatim and in UTC.
    assert first["effective"] == {"text": "2022-01-01T00:00:00+01:00",
                                  "utc": "2021-12-31T23:00:00.000Z"}
    assert first["recorded"]["text"] == "2022-02-01T10:00:00.000+01:00"
    assert first["affected_fields"] == [{"field_xpath": "/LEIRecord/Entity/LegalName",
                                         "value": "Synthetic Alte AG"}]
    assert (second["type"], second["event_status"], second["effective"]) == (
        "LIQUIDATION", "IN_PROGRESS", None)
    assert fields["entity_creation"]["utc"] == "2001-05-03T22:00:00.000Z"
    assert fields["registration"]["last_update"]["text"] == "2026-01-01T09:30:00.000+01:00"
    assert fields["registration"]["validation_authority"] == {"id": "RA000548", "other_id": None,
                                                              "entity_id": None}


def test_stated_null_is_distinct_from_absent_fields(tmp_path):
    fields = one(tmp_path, **SPARSE)["fields"]

    assert fields["entity_status"] == "NULL"                   # stated by the source
    for field in ("entity_category", "legal_form", "entity_creation", "registration_authority",
                  "legal_jurisdiction", "other_entity_names", "successor_entities",
                  "legal_entity_events", "associated_entity"):
        assert fields[field] is None, field                    # not stated
    assert fields["registration"]["validation_sources"] is None
    active = one(tmp_path, **{**SPARSE, "status": "ACTIVE"})
    assert active["record_sha256"] != one(tmp_path, **SPARSE)["record_sha256"]


def test_canonical_lei2_hash_version_1_fixed_values(tmp_path):
    _, (rich, sparse) = parse_records(tmp_path, [record(**RICH), record(**SPARSE)])
    assert (rich["record_sha256"], sparse["record_sha256"]) == (RICH_SHA256, SPARSE_SHA256)
    assert rich["record_hash_version"] == 1 and rich["key"] == A


def test_whitespace_does_not_change_the_hash_but_order_and_text_do(tmp_path):
    base = record(**RICH)
    spaced = base.replace("><", ">\n  <").replace(">Synth Trade<", ">  Synth Trade \n<")
    _, [r1] = parse_records(tmp_path, [base], "a.xml")
    _, [r2] = parse_records(tmp_path, [spaced], "b.xml")
    assert r1["record_sha256"] == r2["record_sha256"]

    swapped = dict(RICH, other_names=tuple(reversed(RICH["other_names"])))
    reordered_events = dict(RICH, events=tuple(reversed(RICH["events"])))
    nfd = dict(RICH, other_names=(("Synthetic Alte AG", "PREVIOUS_LEGAL_NAME", "de"),
                                  ("Synth Tradé", "TRADING_OR_OPERATING_NAME", "en")))
    for variant in (swapped, reordered_events, nfd, dict(RICH, name_lang="en"),
                    dict(RICH, successors=(B,))):
        assert one(tmp_path, **variant)["record_sha256"] != r1["record_sha256"]


def test_addresses_keep_every_stated_part(tmp_path):
    from gleif_lei2_samples import address
    hq = address("HeadquartersAddress", lang="en", line="Synthetic Tower",
                 extra_lines=("Floor 3", "Wing B"), region="CH-ZH", postal=None)
    fields = one(tmp_path, headquarters=hq)["fields"]["headquarters_address"]

    assert fields["additional_address_lines"] == ["Floor 3", "Wing B"]
    assert (fields["lang"], fields["region"], fields["postal_code"]) == ("en", "CH-ZH", None)


def test_invalid_check_digits_are_kept_and_flagged(tmp_path):
    rec = one(tmp_path, lei=BAD, successors=(BAD,))
    assert rec["fields"]["lei"] == BAD and rec["fields"]["successor_entities"][0]["lei"] == BAD
    assert rec["flags"] == [FLAG_INVALID_CHECK_DIGITS]
    with pytest.raises(LEI2FormatError, match="not in LEI form"):
        one(tmp_path, lei="NOT-AN-LEI-0000000000")


@pytest.mark.parametrize("kwargs, message", [
    ({"status": "DORMANT"}, "Unsupported EntityStatus"),
    ({"category": "TRUST"}, "Unsupported EntityCategory"),
    ({"registration_status": "PUBLISHED"}, "Unsupported RegistrationStatus"),   # an RR value
    ({"other_names": (("X", "NICKNAME", "en"),)}, "Unsupported OtherEntityName type"),
    ({"events": (event(kind="REBRANDING"),)}, "Unsupported LegalEntityEventType"),
    ({"events": (event(status="DONE"),)}, "Unsupported LegalEntityEvent event_status"),
    ({"events": (event(extra_attrs=' group_sequence_no="0"'),)}, "group_sequence_no"),
    ({"jurisdiction": "Switzerland"}, "Unsupported LegalJurisdiction"),
    ({"legal_form": ("TOO-LONG", None)}, "Unsupported EntityLegalFormCode"),
    ({"creation": "2001-05-04"}, "time zone"),
    ({"name_lang": "not a language"}, "Unsupported LegalName lang"),
])
def test_unsupported_values_are_rejected(tmp_path, kwargs, message):
    with pytest.raises(LEI2FormatError, match=message):
        one(tmp_path, **kwargs)


@pytest.mark.parametrize("kwargs, message", [
    ({"record_extra": "<lei:NextVersion/>"}, "not supported: NextVersion in LEIRecord"),
    ({"record_extra": "<lei:Extension/>"}, "not supported: Extension in LEIRecord"),
    ({"registration_extra": "<lei:OtherValidationAuthorities/>"},
     "needs 1..n OtherValidationAuthority"),
    ({"entity_extra": '<lei:Bogus/>'}, "Unsupported element Bogus in Entity"),
])
def test_unsupported_structures_are_rejected(tmp_path, kwargs, message):
    with pytest.raises(LEI2FormatError, match=message):
        one(tmp_path, **kwargs)


def test_attributes_only_where_the_xsd_defines_them(tmp_path):
    with pytest.raises(LEI2FormatError, match="Unexpected attributes on EntityStatus"):
        one(tmp_path, status='ACTIVE</lei:EntityStatus><lei:EntityStatus x="1">ACTIVE')
    text = document([record()]).replace("<lei:LegalName ", '<lei:LegalName type="X" ')
    path = tmp_path / "attr.xml"
    path.write_text(text)
    with pytest.raises(LEI2FormatError, match="Unexpected attributes on LegalName"):
        scan_file(path)


def test_successor_must_state_exactly_one_identity(tmp_path):
    both = record().replace(
        "<lei:EntityStatus>", f"<lei:EntityStatus>").replace(
        "</lei:Entity>", f"<lei:SuccessorEntity><lei:SuccessorLEI>{B}</lei:SuccessorLEI>"
                         '<lei:SuccessorEntityName xml:lang="de">X</lei:SuccessorEntityName>'
                         "</lei:SuccessorEntity></lei:Entity>")
    with pytest.raises(LEI2FormatError, match="exactly one of SuccessorLEI or SuccessorEntityName"):
        parse_records(tmp_path, [both])


def test_records_level_extension_and_namespaces(tmp_path):
    with pytest.raises(LEI2FormatError, match="not supported: Extension in LEIRecords"):
        parse_records(tmp_path, [record()], records_extra="<lei:Extension/>")
    with pytest.raises(LEI2FormatError, match="Unsupported LEI namespace"):
        parse_records(tmp_path, [record()], namespace="urn:other")
    rr = tmp_path / "rr.xml"
    rr.write_text(rr_document([relationship(A, B)]))
    with pytest.raises(LEI2FormatError, match="Unsupported LEI namespace"):
        scan_file(rr)


def test_header_extension_is_captured(tmp_path):
    scan, _ = parse_records(tmp_path, [record()],
                            sources=[("2026-10-22T08:00:00Z", LOU, 1)])
    assert scan["header"]["header_extension"] == {
        "namespace": CONCATENATED_HEADER_NS,
        "sources": [{"content_date_text": "2026-10-22T08:00:00Z", "originator": LOU,
                     "record_count": 1}],
        "source_record_counts_match_header": True}


@pytest.mark.parametrize("prolog", ['<!DOCTYPE x [<!ENTITY e "e">]>\n',
                                    '<!DOCTYPE x SYSTEM "http://example.invalid/x.dtd">\n'])
def test_dtds_are_refused(tmp_path, prolog):
    with pytest.raises(LEI2FormatError, match="DTDs and entity declarations"):
        parse_records(tmp_path, [record()], prolog=prolog)


def test_query_responses_and_count_mismatches(tmp_path):
    with pytest.raises(LEI2FormatError, match="no defined dataset scope"):
        parse_records(tmp_path, [record()], file_content="QUERY_RESPONSE")
    with pytest.raises(LEI2FormatError, match="RecordCount says 2"):
        parse_records(tmp_path, [record()], record_count=2)


def test_changed_record_is_refused_in_the_second_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(gleif_lei2, "BLOCK_BYTES", 512)
    path = write(tmp_path / "f.xml", [record(lei=synthetic_lei(f"SYNTHLEI{i}"), name=f"Name {i}")
                                      for i in range(5)])
    scan = scan_file(path)
    stat = os.stat(path)
    seen = []
    with RecordStream(path, scan) as stream, pytest.raises(SourceChangedError,
                                                           match="Record 5 differs"):
        for rec in stream:
            seen.append(rec["ordinal"])
            if rec["ordinal"] == 1:
                path.write_text(path.read_text().replace("Name 4", "Name X"))
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert seen == [1, 2, 3, 4]


def test_parsing_is_streaming_with_bounded_memory(tmp_path, monkeypatch):
    # Memory is bounded by the read block, not the file: compare two files
    # that each span many blocks.
    monkeypatch.setattr(gleif_lei2, "BLOCK_BYTES", 64 * 1024)

    def peak(count):
        path = write(tmp_path / f"m{count}.xml",
                     [record(lei=synthetic_lei(f"SYNTHM{i:06d}"), **RICH) for i in range(count)])
        tracemalloc.start()
        scan_file(path)
        size = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        return size

    small, large = peak(300), peak(3000)
    assert large < 1.5 * small + (1 << 20) and large < 16 << 20


def test_other_validation_authorities_and_typed_addresses(tmp_path):
    from gleif_lei2_samples import address
    extra = ("<lei:OtherValidationAuthorities>" + "".join(
        f"<lei:OtherValidationAuthority><lei:ValidationAuthorityID>{ra}</lei:ValidationAuthorityID>"
        "</lei:OtherValidationAuthority>" for ra in ("RA000001", "RA000002"))
        + "</lei:OtherValidationAuthorities>")
    others = ("<lei:OtherAddresses>" + address("OtherAddress", lang="fr",
                                                type_="ALTERNATIVE_LANGUAGE_LEGAL_ADDRESS")
              + "</lei:OtherAddresses>")
    fields = one(tmp_path, registration_extra=extra,
                 headquarters=address("HeadquartersAddress") + others)["fields"]

    assert [a["id"] for a in fields["registration"]["other_validation_authorities"]] == [
        "RA000001", "RA000002"]                                   # document order
    assert fields["other_addresses"][0]["type"] == "ALTERNATIVE_LANGUAGE_LEGAL_ADDRESS"
    with pytest.raises(LEI2FormatError, match="Unsupported OtherAddress type"):
        one(tmp_path, headquarters=address("HeadquartersAddress") + others.replace(
            "ALTERNATIVE_LANGUAGE_LEGAL_ADDRESS", "POSTAL_ADDRESS"))
