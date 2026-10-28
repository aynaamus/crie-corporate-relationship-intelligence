
"""The read-only GLEIF format probe (23B.2B-3 Stage 3-0). Synthetic files only."""

import hashlib
import json
import os
import socket
import tracemalloc
import zipfile
from pathlib import Path

import pytest

from src import gleif_format_probe
from src.gleif_format_probe import LEI_NS, ProbeError, probe

from gleif_rr_samples import document, relationship, synthetic_lei, write

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gleif"
A, B = synthetic_lei("SYNTHPRA"), synthetic_lei("SYNTHPRB")
SECRET_NAME, SECRET_STREET, SECRET_REFERENCE = (
    "Secretive Holdings AG", "Hauptstrasse 42", "Board minute 2024-17")


def lei_record(lei=A, successor=B, events=True, extension=True, category="GENERAL"):
    return (
        f"<lei:LEIRecord><lei:LEI>{lei}</lei:LEI><lei:Entity>"
        f'<lei:LegalName xml:lang="de">{SECRET_NAME}</lei:LegalName>'
        '<lei:OtherEntityNames><lei:OtherEntityName xml:lang="en" type="TRADING_OR_OPERATING_NAME">'
        "Secretive Trading</lei:OtherEntityName></lei:OtherEntityNames>"
        + "".join(f'<lei:{kind} xml:lang="de"><lei:FirstAddressLine>{SECRET_STREET}</lei:FirstAddressLine>'
                  f"<lei:City>Zug</lei:City><lei:Country>CH</lei:Country>"
                  f"<lei:PostalCode>6300</lei:PostalCode></lei:{kind}>"
                  for kind in ("LegalAddress", "HeadquartersAddress"))
        + "<lei:RegistrationAuthority><lei:RegistrationAuthorityID>RA000548</lei:RegistrationAuthorityID>"
        "<lei:RegistrationAuthorityEntityID>CHE-123.456.789</lei:RegistrationAuthorityEntityID>"
        "</lei:RegistrationAuthority><lei:LegalJurisdiction>CH</lei:LegalJurisdiction>"
        f"<lei:EntityCategory>{category}</lei:EntityCategory>"
        "<lei:LegalForm><lei:EntityLegalFormCode>MVII</lei:EntityLegalFormCode></lei:LegalForm>"
        "<lei:EntityStatus>ACTIVE</lei:EntityStatus>"
        "<lei:EntityCreationDate>2001-05-04T00:00:00+02:00</lei:EntityCreationDate>"
        f"<lei:SuccessorEntity><lei:SuccessorLEI>{successor}</lei:SuccessorLEI></lei:SuccessorEntity>"
        + ('<lei:LegalEntityEvents><lei:LegalEntityEvent group_type="STANDALONE" event_status="COMPLETED">'
           "<lei:LegalEntityEventType>CHANGE_LEGAL_NAME</lei:LegalEntityEventType>"
           "<lei:LegalEntityEventEffectiveDate>2022-01-01T00:00:00Z</lei:LegalEntityEventEffectiveDate>"
           "<lei:LegalEntityEventRecordedDate>2022-02-01T00:00:00Z</lei:LegalEntityEventRecordedDate>"
           "<lei:ValidationDocuments>SUPPORTING_DOCUMENTS</lei:ValidationDocuments>"
           f"<lei:ValidationReference>{SECRET_REFERENCE}</lei:ValidationReference>"
           "</lei:LegalEntityEvent></lei:LegalEntityEvents>" if events else "")
        + "</lei:Entity><lei:Registration>"
        "<lei:InitialRegistrationDate>2014-01-01T00:00:00Z</lei:InitialRegistrationDate>"
        "<lei:LastUpdateDate>2026-01-01T00:00:00Z</lei:LastUpdateDate>"
        "<lei:RegistrationStatus>ISSUED</lei:RegistrationStatus>"
        "<lei:NextRenewalDate>2027-01-01T00:00:00Z</lei:NextRenewalDate>"
        f"<lei:ManagingLOU>{B}</lei:ManagingLOU>"
        "<lei:ValidationSources>FULLY_CORROBORATED</lei:ValidationSources></lei:Registration>"
        + ('<lei:Extension><geo:Geocoding xmlns:geo="urn:synthetic:geocoding">'
           "<geo:Latitude>47.17</geo:Latitude></geo:Geocoding></lei:Extension>" if extension else "")
        + "</lei:LEIRecord>")


def lei_document(records, schema_location=None):
    location = (' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                f'xsi:schemaLocation="{LEI_NS} {schema_location}"') if schema_location else ""
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<lei:LEIData xmlns:lei="{LEI_NS}"{location}><lei:LEIHeader>'
            "<lei:ContentDate>2026-10-22T08:00:00Z</lei:ContentDate>"
            "<lei:FileContent>GLEIF_FULL_PUBLISHED</lei:FileContent>"
            f"<lei:RecordCount>{len(records)}</lei:RecordCount></lei:LEIHeader>"
            "<lei:LEIRecords>" + "".join(records) + "</lei:LEIRecords></lei:LEIData>")


def lei_file(tmp_path, records, **kwargs):
    path = tmp_path / "synthetic-lei2.xml"
    path.write_text(lei_document(records, **kwargs), encoding="utf-8")
    return path


def test_rr_root_namespace_and_unstated_version():
    report = probe(FIXTURES / "rr_cdf_1_1_synthetic.xml")

    assert report["root"]["namespace"] == "http://www.gleif.org/data/schema/rr/2016"
    assert report["detected"]["family"] == "RR-CDF"
    assert report["detected"]["version_stated_in_file"] is False
    assert report["detected"]["versions_sharing_namespace"] == ["RR-CDF 1.1", "RR-CDF 2.1"]
    assert "not stated in the file" in report["detected"]["conclusion"]
    assert report["header"]["values"]["FileContent"] == "GLEIF_FULL_PUBLISHED"
    assert report["installed_rr_parser"]["would_accept_all_inspected_records"] is True


def test_version_features_are_evidence_not_proof(tmp_path):
    path = write(tmp_path / "f.xml", [relationship(A, B, kind="IS_FEEDER_TO"),
                                      relationship(B, A, status="NULL")])
    detected = probe(path)["detected"]

    assert detected["version_features_observed"] == {"RelationshipType = IS_FEEDER_TO": 1,
                                                     "RelationshipStatus = NULL": 1}
    assert "suggest RR-CDF 2.1 or later; this is evidence, not proof" in detected["conclusion"]


def test_rr_2_1_structures_against_the_installed_parser(tmp_path):
    text = document([relationship(A, B, child_type="ISO_17442_COMPATIBLE",
                                  quantifiers=(("ACCOUNTING_CONSOLIDATION", "-5", "PERCENTAGE"),
                                               ("ACCOUNTING_CONSOLIDATION", "150", "PERCENTAGE")),
                                  extra='<rr:Extension><x:Note xmlns:x="urn:synthetic:ext">n</x:Note>'
                                        "</rr:Extension>")])
    text = (text.replace("<rr:QualifierCategory>IFRS</rr:QualifierCategory>", "")
            .replace("<rr:NextRenewalDate>2027-03-01T08:00:00Z</rr:NextRenewalDate>", "")
            .replace("</rr:RelationshipQualifiers>",
                     "</rr:RelationshipQualifiers><rr:RelationshipQualifiers>"
                     "<rr:RelationshipQualifier><rr:QualifierDimension>ACCOUNTING_STANDARD"
                     "</rr:QualifierDimension></rr:RelationshipQualifier></rr:RelationshipQualifiers>"))
    path = tmp_path / "rr21.xml"
    path.write_text(text)
    report = probe(path)
    parser = report["installed_rr_parser"]

    # Still rejected: no lossless form, or not supported at all.
    assert parser["would_accept_all_inspected_records"] is False
    assert set(parser["rejections"]) == {
        "element not accepted: RelationshipRecord/Extension",
        "occurrence not accepted: RelationshipRecord/Relationship/RelationshipQualifiers x2 "
        "(installed parser: 0..1)"}
    # Accepted as stated, flagged for attention (negative amounts are valid).
    assert parser["flags_raised"] == {
        "provisional node identifier: RelationshipRecord/Relationship/StartNode/NodeIDType": 1,
        "QuantifierAmount over 100 (flagged when units are PERCENTAGE)": 1}
    # All of these are valid in the official RR-CDF 2.1 structure.
    assert report["not_in_official_structure"] == {}
    assert report["extensions"]["RelationshipRecord/Extension"] == {
        "namespaces": {"urn:synthetic:ext": 1}, "elements": {"Note": 1}}
    assert report["quantifier_amounts"]["negative"] == 1


def test_unknown_elements_and_foreign_namespaces_are_flagged(tmp_path):
    text = document([relationship(A, B)]).replace(
        "<rr:RelationshipStatus>", '<rr:Bogus>x</rr:Bogus><o:Other xmlns:o="urn:other"/>'
                                   "<rr:RelationshipStatus>")
    path = tmp_path / "unknown.xml"
    path.write_text(text)
    report = probe(path)

    assert set(report["not_in_official_structure"]) == {
        "RelationshipRecord/Relationship/Bogus", "RelationshipRecord/Relationship/Other"}
    assert report["elements_outside_document_namespace"] == {"RelationshipRecord/Relationship/Other": 1}
    assert "element not accepted: RelationshipRecord/Relationship/Bogus" in \
        report["installed_rr_parser"]["rejections"]


def test_lei2_attributes_extensions_codes_and_fields(tmp_path):
    report = probe(lei_file(tmp_path, [lei_record(), lei_record(events=False, extension=False)],
                            schema_location="https://www.gleif.org/2021-03-04_lei-cdf-v3-1.xsd"))

    assert report["detected"]["family"] == "LEI-CDF"
    assert report["detected"]["schema_location_documents"] == ["2021-03-04_lei-cdf-v3-1.xsd"]
    assert "suggest LEI-CDF 3.x or later" in report["detected"]["conclusion"]
    assert "declaration, not validation" in report["detected"]["conclusion"]
    attributes = report["attributes"]
    assert attributes["LEIRecord/Entity/LegalName@xml:lang"] == 2
    assert attributes["LEIRecord/Entity/LegalEntityEvents/LegalEntityEvent@group_type"] == 1
    codes = report["code_values"]
    assert codes["LEIRecord/Entity/OtherEntityNames/OtherEntityName@type"] == {
        "TRADING_OR_OPERATING_NAME": 2}
    assert codes["LEIRecord/Entity/LegalEntityEvents/LegalEntityEvent@event_status"] == {"COMPLETED": 1}
    assert codes["LEIRecord/Registration/RegistrationStatus"] == {"ISSUED": 2}
    assert codes["LEIRecord/Entity/LegalAddress/Country"] == {"CH": 2}
    assert report["extensions"]["LEIRecord/Extension"]["namespaces"] == {"urn:synthetic:geocoding": 2}
    fields = report["proposed_lei2_fields_present"]
    assert fields["Entity/LegalEntityEvents"] == 1 and fields["Entity/LegalName@xml:lang"] == 2
    assert fields["Extension"] == 1 and fields["Entity/EntitySubCategory"] == 0
    assert report["identifier_checks"]["LEIRecord/LEI"] == {"valid ISO 17442 check digits": 2}
    assert "installed_rr_parser" not in report
    # Address internals are not modelled by the probe: listed, never judged.
    assert "LEIRecord/Entity/LegalAddress" in report["containers_not_modelled_by_probe"]


def test_no_record_level_identifying_values_are_disclosed(tmp_path):
    text = json.dumps(probe(lei_file(tmp_path, [lei_record()])))
    rr_text = json.dumps(probe(write(tmp_path / "rr.xml", [relationship(
        A, B, reference=SECRET_REFERENCE)])))

    for secret in (A, B, SECRET_NAME, "Secretive Trading", SECRET_STREET, SECRET_REFERENCE,
                   "CHE-123.456.789", "6300", "Zug", "47.17"):
        assert secret not in text, secret
        assert secret not in rr_text, secret


def test_code_like_values_on_code_fields_only_and_leis_withheld(tmp_path):
    record = lei_record().replace("<lei:EntityCategory>GENERAL", f"<lei:EntityCategory>{A}")
    codes = probe(lei_file(tmp_path, [record]))["code_values"]
    assert codes["LEIRecord/Entity/EntityCategory"] == {"<LEI-shaped value withheld>": 1}


@pytest.mark.parametrize("prolog", [
    '<!DOCTYPE lol [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>\n',
    '<!DOCTYPE x [<!ENTITY ext SYSTEM "file:///etc/passwd">]>\n',
    '<!DOCTYPE x SYSTEM "http://example.invalid/rr.dtd">\n',
])
def test_dtds_and_external_entities_are_refused(tmp_path, prolog):
    with pytest.raises(ProbeError, match="DTDs and entity declarations"):
        probe(write(tmp_path / "unsafe.xml", [relationship(A, B)], prolog=prolog))


def test_malformed_and_unrecognized_documents(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text(document([relationship(A, B)]).replace("</rr:Registration>", "</rr:Registratio>"))
    with pytest.raises(ProbeError, match="Malformed XML"):
        probe(bad)

    other = tmp_path / "other.xml"
    other.write_text('<?xml version="1.0"?><root xmlns="urn:other"><child/></root>')
    report = probe(other)
    assert report["detected"]["namespace_recognized"] is False
    assert report["root"] == {"namespace": "urn:other", "element": "root", "attributes": [],
                              "schema_location": []}
    assert report["paths"] == {} and report["xml_declaration"]["version"] == "1.0"


def test_sample_limit_stops_reading_and_full_mode_checks_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(gleif_format_probe, "BLOCK_BYTES", 4096)
    path = write(tmp_path / "many.xml", [relationship(A, B, reference=f"R{i}") for i in range(300)])

    sample = probe(path, max_records=5)
    assert sample["records_inspected"] == 5 and sample["mode"].startswith("SAMPLE")
    assert sample["source"]["read_to_end"] is False
    assert sample["source"]["xml_bytes_read"] < path.stat().st_size / 4
    assert "full_file" not in sample

    full = probe(path, full=True)
    assert full["mode"] == "FULL" and full["records_inspected"] == 300
    assert full["full_file"] == {"xml_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                 "header_record_count": "300", "records_counted": 300,
                                 "record_count_matches": True}

    wrong = write(tmp_path / "wrong.xml", [relationship(A, B)], record_count=3)
    assert probe(wrong, full=True)["full_file"]["record_count_matches"] is False
    with pytest.raises(ValueError):
        probe(path, max_records=0)


def test_bounded_memory(tmp_path):
    def peak(count):
        path = write(tmp_path / f"m{count}.xml",
                     [relationship(A, B, reference=f"Ref {i:05d}") for i in range(count)])
        tracemalloc.start()
        probe(path, full=True)
        size = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        return size

    small, large = peak(300), peak(3000)
    assert large < 1.5 * small + (1 << 20) and large < 16 << 20


def test_zip_member_is_streamed_without_extraction(tmp_path):
    xml = document([relationship(A, B)])
    archive = tmp_path / "rr.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
        handle.writestr("synthetic-rr.xml", xml)
    before = sorted(os.listdir(tmp_path))

    report = probe(archive, full=True)
    assert report["source"]["container"]["type"] == "zip"
    assert report["source"]["container"]["member"] == "synthetic-rr.xml"
    assert report["full_file"]["xml_sha256"] == hashlib.sha256(xml.encode()).hexdigest()
    assert sorted(os.listdir(tmp_path)) == before

    two = tmp_path / "two.zip"
    with zipfile.ZipFile(two, "w") as handle:
        handle.writestr("a.xml", xml)
        handle.writestr("b.xml", xml)
    with pytest.raises(ProbeError, match="exactly one"):
        probe(two)


def test_no_database_no_network_and_source_untouched(tmp_path, monkeypatch):
    import mysql.connector

    def forbidden(*args, **kwargs):
        raise AssertionError("the probe must not open connections")

    monkeypatch.setattr(mysql.connector, "connect", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    path = lei_file(tmp_path, [lei_record()])
    stat, content = path.stat(), path.read_bytes()

    probe(path, full=True)
    assert path.read_bytes() == content and path.stat().st_mtime_ns == stat.st_mtime_ns
    assert sorted(os.listdir(tmp_path)) == ["synthetic-lei2.xml"]


def test_cli_output_rules(tmp_path, capsys):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    path = lei_file(source_dir, [lei_record()])

    assert gleif_format_probe.main([str(path), "--max-records", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["records_inspected"] == 1
    with pytest.raises(SystemExit):
        gleif_format_probe.main([str(path), "--output", str(source_dir / "report.json")])
    report = tmp_path / "report.json"
    gleif_format_probe.main([str(path), "--output", str(report)])
    assert json.loads(report.read_text())["detected"]["family"] == "LEI-CDF"
    with pytest.raises(FileExistsError):
        gleif_format_probe.main([str(path), "--output", str(report)])
    with pytest.raises(SystemExit):
        gleif_format_probe.main([str(path), "--full", "--max-records", "3"])


def test_long_official_code_values_are_reported(tmp_path):
    # The official 41-character transliteration type must not be withheld
    # as non-code text (the pattern once stopped at 40 characters).
    record = lei_record().replace(
        "</lei:OtherEntityNames>",
        "</lei:OtherEntityNames><lei:TransliteratedOtherEntityNames>"
        '<lei:TransliteratedOtherEntityName xml:lang="de" '
        'type="PREFERRED_ASCII_TRANSLITERATED_LEGAL_NAME">Secretive Holdings AG'
        "</lei:TransliteratedOtherEntityName></lei:TransliteratedOtherEntityNames>")
    report = probe(lei_file(tmp_path, [record]))

    assert report["code_values"][
        "LEIRecord/Entity/TransliteratedOtherEntityNames/TransliteratedOtherEntityName@type"] == {
        "PREFERRED_ASCII_TRANSLITERATED_LEGAL_NAME": 1}
    assert SECRET_NAME not in json.dumps(report)
    assert gleif_format_probe._CODE.match("A" * 64) and not gleif_format_probe._CODE.match("A" * 65)
