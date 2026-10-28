
"""
RR-CDF 2.1 compatibility of the RR parser (23B.2B-3a): what is accepted
within record-hash version 1, what is flagged, what stays rejected, and
how the importer refuses what it cannot yet store. No database.
"""

import pytest

from src.gleif_import import ImportRefused, import_rr_file
from src.gleif_observations import canonical_selection
from src.gleif_rr import (
    FLAG_INVALID_CHECK_DIGITS,
    FLAG_OVER_100_PERCENT,
    FLAG_PROVISIONAL_NODE,
    RecordStream,
    RRFormatError,
    scan_file,
)
from src.gleif_xml import CONCATENATED_HEADER_NS

from gleif_rr_samples import LOU, document, relationship, synthetic_lei

A, B = synthetic_lei("SYNTHCMA"), synthetic_lei("SYNTHCMB")
BAD = A[:-2] + f"{(int(A[-2:]) + 1) % 100:02d}"            # LEI form, wrong check digits
NULL_STATUS_SHA256 = "5010daf4b7558dbd3b0e96e95074819996fd372771320284a70b6ba12432f89d"
NO_CATEGORY = ("<rr:QualifierCategory>IFRS</rr:QualifierCategory>", "")
NO_RENEWAL = ("<rr:NextRenewalDate>2027-03-01T08:00:00Z</rr:NextRenewalDate>", "")


def parse(tmp_path, records, *replacements, **header):
    text = document(records, **header)
    for old, new in replacements:
        assert old in text, old
        text = text.replace(old, new)
    path = tmp_path / "rr.xml"
    path.write_text(text)
    scan = scan_file(path)
    with RecordStream(path, scan) as stream:
        found = list(stream)
        stream.verify()
    return scan, found


def extension(*sources, namespace=CONCATENATED_HEADER_NS, extra=""):
    entries = "".join(
        f"<gleif:Source><gleif:ContentDate>{d}</gleif:ContentDate>"
        f"<gleif:Originator>{o}</gleif:Originator><gleif:RecordCount>{n}</gleif:RecordCount>"
        f"{extra}</gleif:Source>" for d, o, n in sources)
    return ("</rr:RecordCount></rr:Header>",
            f'</rr:RecordCount><rr:Extension><gleif:Sources xmlns:gleif="{namespace}">'
            f"{entries}</gleif:Sources></rr:Extension></rr:Header>")


def test_null_status_absent_category_and_renewal_within_hash_version_1(tmp_path):
    _, [record] = parse(tmp_path, [relationship(A, B, status="NULL", registration="ANNULLED")],
                        NO_CATEGORY, NO_RENEWAL)
    fields = record["fields"]

    assert fields["relationship_status"] == "NULL"           # stated, not absent
    assert fields["qualifiers"] == [{"dimension": "ACCOUNTING_STANDARD", "category": None}]
    assert fields["next_renewal_at_utc"] is None
    assert record["record_hash_version"] == 1 and record["flags"] == []
    assert record["record_sha256"] == NULL_STATUS_SHA256       # pinned, version 1


def test_stated_null_differs_from_other_statuses(tmp_path):
    hashes = {status: parse(tmp_path, [relationship(A, B, status=status)])[1][0]["record_sha256"]
              for status in ("NULL", "ACTIVE", "INACTIVE")}
    assert len(set(hashes.values())) == 3


def test_empty_qualifier_container_is_not_an_absent_one(tmp_path):
    empty = ("<rr:RelationshipQualifiers><rr:RelationshipQualifier><rr:QualifierDimension>"
             "ACCOUNTING_STANDARD</rr:QualifierDimension><rr:QualifierCategory>IFRS"
             "</rr:QualifierCategory></rr:RelationshipQualifier></rr:RelationshipQualifiers>",
             "<rr:RelationshipQualifiers></rr:RelationshipQualifiers>")
    _, [with_empty] = parse(tmp_path, [relationship(A, B)], empty)
    _, [without] = parse(tmp_path, [relationship(A, B, qualifiers=())])

    assert with_empty["fields"]["qualifiers"] == [] and without["fields"]["qualifiers"] is None
    assert with_empty["record_sha256"] != without["record_sha256"]


def test_amounts_are_kept_exactly_and_implausible_percentages_flagged(tmp_path):
    _, [record] = parse(tmp_path, [relationship(A, B, quantifiers=(
        ("ACCOUNTING_CONSOLIDATION", "-5.25", "PERCENTAGE"),
        ("ACCOUNTING_CONSOLIDATION", "150.000001", "PERCENTAGE"),
        ("ACCOUNTING_CONSOLIDATION", "250", None)))])

    assert [q["amount"] for q in record["fields"]["quantifiers"]] == ["-5.25", "150.000001", "250"]
    assert record["flags"] == [FLAG_OVER_100_PERCENT]          # only the stated percentage


def test_provisional_and_invalid_identifiers_are_kept_and_flagged(tmp_path):
    _, [provisional, invalid] = parse(tmp_path, [
        relationship(A, B, child_type="ISO_17442_COMPATIBLE"), relationship(BAD, B)])

    assert provisional["fields"]["start_node_id_type"] == "ISO_17442_COMPATIBLE"
    assert provisional["flags"] == [FLAG_PROVISIONAL_NODE]
    assert invalid["fields"]["child_lei"] == BAD                # never corrected
    assert invalid["flags"] == [FLAG_INVALID_CHECK_DIGITS]


@pytest.mark.parametrize("replacement, message", [
    (("</rr:RelationshipQualifiers>",
      "</rr:RelationshipQualifiers><rr:RelationshipQualifiers></rr:RelationshipQualifiers>"),
     "2 RelationshipQualifiers in Relationship .no lossless canonical form"),
    (("</rr:RelationshipQuantifiers>",
      "</rr:RelationshipQuantifiers><rr:RelationshipQuantifiers></rr:RelationshipQuantifiers>"),
     "2 RelationshipQuantifiers in Relationship .no lossless canonical form"),
    (("</rr:Registration>", "</rr:Registration><rr:NextVersion/>"),
     "not supported: NextVersion in RelationshipRecord"),
    (("</rr:RelationshipStatus>", "</rr:RelationshipStatus><rr:NextVersion/>"),
     "not supported: NextVersion in Relationship"),
    (("</rr:ValidationDocuments>", "</rr:ValidationDocuments><rr:Extension/>"),
     "not supported: Extension in Registration"),
    (("</rr:Registration>", '</rr:Registration><rr:Extension><x:A xmlns:x="urn:x"/></rr:Extension>'),
     "not supported: Extension content outside the header"),
    (("</rr:RecordCount>", "</rr:RecordCount><rr:NextVersion/>"),
     "not supported: NextVersion in Header"),
])
def test_valid_but_unrepresentable_structures_stay_rejected(tmp_path, replacement, message):
    with pytest.raises(RRFormatError, match=message):
        parse(tmp_path, [relationship(A, B)], replacement)


@pytest.mark.parametrize("file_content, message", [
    ("GLEIF_FULL_INTERNAL", "internal, non-public"), ("QUERY_RESPONSE", "no defined dataset scope")])
def test_internal_files_and_query_responses_are_not_supported(tmp_path, file_content, message):
    with pytest.raises(RRFormatError, match=message):
        parse(tmp_path, [relationship(A, B)], file_content=file_content)


def test_header_extension_is_kept_in_order_at_file_level(tmp_path):
    sources = (("2026-10-22T08:00:00Z", LOU, 1), ("2026-10-21T08:30:00.000+02:00", B, 0))
    scan, [record] = parse(tmp_path, [relationship(A, B)], extension(*sources))

    assert scan["header"]["header_extension"] == {
        "namespace": CONCATENATED_HEADER_NS,
        "sources": [{"content_date_text": d, "originator": o, "record_count": n}
                    for d, o, n in sources],
        "source_record_counts_match_header": True}
    # Header metadata is not record evidence: the record hash is unaffected.
    _, [plain] = parse(tmp_path, [relationship(A, B)])
    assert record["record_sha256"] == plain["record_sha256"]


def test_header_extension_count_mismatch_is_recorded_not_hidden(tmp_path):
    scan, _ = parse(tmp_path, [relationship(A, B)], extension(("2026-10-22T08:00:00Z", LOU, 5)))
    assert scan["header"]["header_extension"]["source_record_counts_match_header"] is False


@pytest.mark.parametrize("kwargs, message", [
    ({"namespace": "urn:other"}, "Unsupported header extension namespace"),
    ({"extra": "<gleif:Note>x</gleif:Note>"}, "Unsupported header extension element: Source/Note"),
])
def test_unknown_header_extension_content_is_rejected(tmp_path, kwargs, message):
    with pytest.raises(RRFormatError, match=message):
        parse(tmp_path, [relationship(A, B)],
              extension(("2026-10-22T08:00:00Z", LOU, 1), **kwargs))


def test_invalid_originator_is_rejected(tmp_path):
    with pytest.raises(RRFormatError, match="Originator is not a valid LEI"):
        parse(tmp_path, [relationship(A, B)],
              ("<rr:FileContent>", f"<rr:Originator>{BAD}</rr:Originator><rr:FileContent>"))


class _NoQuarantineSchema:
    """A test database connection whose schema predates m23b2b3."""

    def __init__(self):
        self.log = []

    def cursor(self, **_):
        log = self.log

        class Cursor:
            def execute(self, sql, params=()):
                log.append(sql)
                self.sql = sql

            def fetchone(self):
                return ("corporate_intelligence_test",) if "DATABASE()" in self.sql else (0,)

            def close(self):
                pass
        return Cursor()

    def rollback(self):
        pass

    def close(self):
        pass


def test_importer_refuses_a_database_without_the_quarantine_schema(tmp_path):
    path = tmp_path / "rr.xml"
    path.write_text(document([relationship(BAD, B)]))
    connection = _NoQuarantineSchema()
    with pytest.raises(ImportRefused, match="lacks migration m23b2b3"):
        import_rr_file(lambda: connection, path, canonical_selection("RR", "ALL_RECORDS"),
                       started_by="synthetic test")
    assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in connection.log)


def test_first_pass_counts_record_flags(tmp_path):
    path = tmp_path / "rr.xml"
    path.write_text(document([relationship(A, B, quantifiers=(
        ("ACCOUNTING_CONSOLIDATION", "150", "PERCENTAGE"),)), relationship(BAD, B)]))
    assert scan_file(path)["record_flags"] == {FLAG_OVER_100_PERCENT: 1, FLAG_INVALID_CHECK_DIGITS: 1}
