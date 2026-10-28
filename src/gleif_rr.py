
"""
GLEIF Relationship Record (RR-CDF) parser (Milestones 23B.2B-2 and -3a),
on the shared safe streaming core in src/gleif_xml.py. Pure: reads a
local file, touches no database and never the network.

Format: the rr/2016 namespace, which RR-CDF 1.1 and 2.1 share; files do
not state their version. The structures of RR-CDF 2.1 (XSD 2021-03-04)
are accepted where canonical record-hash version 1 represents them
without loss: RelationshipStatus NULL, an absent QualifierCategory,
ISO 17442-compatible (provisional) node identifiers, negative amounts, an
absent NextRenewalDate and the concatenated-file header extension.

Explicitly rejected as valid but unsupported: repeated qualifier or
quantifier containers (no lossless version-1 form), NextVersion,
record-level Extension, internal files and query responses. Anything
else unexpected is rejected rather than guessed.

Flags (never part of the hash, never corrections): identifiers that fail
ISO 17442 check digits, provisional node identifiers and percentages over
100 are kept exactly as stated and flagged for the importer and readers.
"""

import re

from src import gleif_xml
from src.claim_subjects import lei_is_valid
from src.gleif_observations import RECORD_HASH_VERSION, record_sha256
from src.gleif_xml import SourceChangedError, utc_text  # noqa: F401  (public names)

RR_NAMESPACES = {"http://www.gleif.org/data/schema/rr/2016":
                 "RR-CDF (rr/2016 namespace; version not stated in the file)"}

BLOCK_BYTES = 1 << 20
MAX_DEPTH = 16
MAX_RECORD_ELEMENTS = 1000
MAX_TEXT_CHARS = 8192

FILE_CONTENT = {
    "GLEIF_FULL_PUBLISHED": "FULL",
    "LOU_FULL_PUBLISHED": "FULL",
    "GLEIF_DELTA_PUBLISHED": "DELTA",
    "LOU_DELTA_PUBLISHED": "DELTA",
}
UNSUPPORTED_FILE_CONTENT = {
    "LOU_FULL_INTERNAL": "an internal, non-public file",
    "LOU_DELTA_INTERNAL": "an internal, non-public file",
    "GLEIF_FULL_INTERNAL": "an internal, non-public file",
    "GLEIF_DELTA_INTERNAL": "an internal, non-public file",
    "QUERY_RESPONSE": "a query response has no defined dataset scope",
}
RELATIONSHIP_TYPES = {
    "IS_DIRECTLY_CONSOLIDATED_BY", "IS_ULTIMATELY_CONSOLIDATED_BY",
    "IS_INTERNATIONAL_BRANCH_OF", "IS_FUND-MANAGED_BY", "IS_SUBFUND_OF", "IS_FEEDER_TO",
}
# "NULL" is a value the source states, distinct from an absent element.
RELATIONSHIP_STATUSES = {"ACTIVE", "INACTIVE", "NULL"}
REGISTRATION_STATUSES = {
    "PENDING_VALIDATION", "PUBLISHED", "DUPLICATE", "LAPSED", "RETIRED", "ANNULLED",
    "PENDING_TRANSFER", "PENDING_ARCHIVAL", "TRANSFERRED",
}
VALIDATION_SOURCES = {"PENDING", "ENTITY_SUPPLIED_ONLY", "PARTIALLY_CORROBORATED",
                      "FULLY_CORROBORATED"}
VALIDATION_DOCUMENTS = {"ACCOUNTS_FILING", "REGULATORY_FILING", "SUPPORTING_DOCUMENTS",
                        "CONTRACTS", "OTHER_OFFICIAL_DOCUMENTS"}
PERIOD_TYPES = {"ACCOUNTING_PERIOD", "RELATIONSHIP_PERIOD", "DOCUMENT_FILING_PERIOD"}
QUALIFIER_DIMENSIONS = {"ACCOUNTING_STANDARD"}
QUALIFIER_CATEGORIES = {"US_GAAP", "IFRS", "OTHER_ACCOUNTING_STANDARD",
                        "GOVERNMENT_ACCOUNTING_STANDARD"}
MEASUREMENT_METHODS = {"ACCOUNTING_CONSOLIDATION"}
QUANTIFIER_UNITS = {"PERCENTAGE"}
NODE_ID_TYPES = {"LEI", "ISO_17442_COMPATIBLE"}

# Record flags: stated values that need attention, never corrected.
FLAG_INVALID_CHECK_DIGITS = "IDENTIFIER_FAILS_ISO_17442_CHECK_DIGITS"
FLAG_PROVISIONAL_NODE = "PROVISIONAL_NODE_IDENTIFIER"
FLAG_OVER_100_PERCENT = "QUANTIFIER_OVER_100_PERCENT"

# Allowed children, in schema order, with (minimum, maximum) occurrences.
STRUCTURE = {
    "Header": [("ContentDate", 1, 1), ("Originator", 0, 1), ("FileContent", 1, 1),
               ("DeltaStart", 0, 1), ("RecordCount", 1, 1), ("Extension", 0, 1)],
    "RelationshipRecord": [("Relationship", 1, 1), ("Registration", 1, 1)],
    "Relationship": [("StartNode", 1, 1), ("EndNode", 1, 1), ("RelationshipType", 1, 1),
                     ("RelationshipPeriods", 0, 1), ("RelationshipStatus", 1, 1),
                     ("RelationshipQualifiers", 0, 1), ("RelationshipQuantifiers", 0, 1)],
    "StartNode": [("NodeID", 1, 1), ("NodeIDType", 1, 1)],
    "EndNode": [("NodeID", 1, 1), ("NodeIDType", 1, 1)],
    "RelationshipPeriods": [("RelationshipPeriod", 1, None)],
    "RelationshipPeriod": [("StartDate", 0, 1), ("EndDate", 0, 1), ("PeriodType", 1, 1)],
    "RelationshipQualifiers": [("RelationshipQualifier", 0, None)],
    "RelationshipQualifier": [("QualifierDimension", 1, 1), ("QualifierCategory", 0, 1)],
    "RelationshipQuantifiers": [("RelationshipQuantifier", 0, None)],
    "RelationshipQuantifier": [("MeasurementMethod", 1, 1), ("QuantifierAmount", 1, 1),
                               ("QuantifierUnits", 0, 1)],
    "Registration": [("InitialRegistrationDate", 1, 1), ("LastUpdateDate", 1, 1),
                     ("RegistrationStatus", 1, 1), ("NextRenewalDate", 0, 1),
                     ("ManagingLOU", 1, 1), ("ValidationSources", 0, 1),
                     ("ValidationDocuments", 0, 1), ("ValidationReference", 0, 1)],
}
_NOT_IN_V1 = "no lossless canonical form in record-hash version 1"
UNSUPPORTED = {
    ("Header", "NextVersion"): "NextVersion content is not supported",
    ("RelationshipRecord", "NextVersion"): "NextVersion content is not supported",
    ("RelationshipRecord", "Extension"): "record-level extensions are not supported",
    ("Relationship", "NextVersion"): "NextVersion content is not supported",
    ("Relationship", "Extension"): "relationship-level extensions are not supported",
    ("Registration", "NextVersion"): "NextVersion content is not supported",
    ("Registration", "Extension"): "registration-level extensions are not supported",
    ("Relationship", "RelationshipQualifiers", "repeated"): _NOT_IN_V1,
    ("Relationship", "RelationshipQuantifiers", "repeated"): _NOT_IN_V1,
}

_DATETIME = gleif_xml._DATETIME
_CODE = re.compile(r"^[A-Z0-9_]{1,40}$")
_IDENTIFIER = re.compile(r"^[0-9A-Z]{18}[0-9]{2}$")
_DECIMAL = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")


class RRFormatError(gleif_xml.GleifFormatError):
    """The file is malformed, unsafe or outside the supported RR format."""


SPEC = gleif_xml.DocumentSpec(
    "RR", RR_NAMESPACES, "RelationshipData", "Header", "RelationshipRecords",
    "RelationshipRecord", RRFormatError, max_depth=MAX_DEPTH,
    max_record_elements=MAX_RECORD_ELEMENTS, max_text_chars=MAX_TEXT_CHARS)


def _shape(node):
    return gleif_xml.shape(node, STRUCTURE, RRFormatError, UNSUPPORTED)


def _text(groups, tag, allowed=None, required=True):
    return gleif_xml.text(groups, tag, RRFormatError, allowed, required)


def parse_datetime(text, label):
    return gleif_xml.parse_datetime(text, label, RRFormatError)


def _header(node):
    groups = _shape(node)
    content_date_text = _text(groups, "ContentDate")
    file_content = _text(groups, "FileContent")
    if file_content in UNSUPPORTED_FILE_CONTENT:
        raise RRFormatError(f"Valid GLEIF structure not supported: FileContent {file_content} "
                            f"({UNSUPPORTED_FILE_CONTENT[file_content]})")
    if file_content not in FILE_CONTENT:
        raise RRFormatError(f"Unsupported FileContent: {file_content!r}")
    scope = FILE_CONTENT[file_content]
    delta_start = _text(groups, "DeltaStart", required=False)
    if (scope == "DELTA") != (delta_start is not None):
        raise RRFormatError("DeltaStart is required for, and only for, a delta file")
    if delta_start is not None:
        parse_datetime(delta_start, "DeltaStart")
    count_text = _text(groups, "RecordCount")
    if not count_text.isdigit():
        raise RRFormatError(f"RecordCount is not a whole number: {count_text!r}")
    originator = _text(groups, "Originator", required=False)
    if originator is not None:
        _lei(originator, "Originator")
    extension = None
    if "Extension" in groups:
        extension = gleif_xml.header_extension(groups["Extension"][0], RRFormatError)
        extension["source_record_counts_match_header"] = (
            sum(s["record_count"] for s in extension["sources"]) == int(count_text))
    header = {
        "content_date_text": content_date_text,
        "content_date_utc": parse_datetime(content_date_text, "ContentDate"),
        "originator": originator,
        "file_content_text": file_content,
        "publication_scope": scope,
        "delta_basis": delta_start,
        "header_record_count": int(count_text),
    }
    if extension is not None:
        header["header_extension"] = extension
    return header


def _lei(value, label):
    """A strictly valid LEI (used where no flagging is defined)."""

    if not lei_is_valid(value):
        raise RRFormatError(f"{label} is not a valid LEI: {value!r}")
    return value


def _identifier(value, label, flags):
    """An identifier in LEI form; failing check digits are flagged, not fixed."""

    if not _IDENTIFIER.match(value):
        raise RRFormatError(f"{label} is not a valid LEI: {value!r}")
    if not lei_is_valid(value):
        flags.add(FLAG_INVALID_CHECK_DIGITS)
    return value


def _node(groups, tag, flags):
    node_groups = _shape(groups[tag][0])
    kind = _text(node_groups, "NodeIDType")
    if kind not in NODE_ID_TYPES:
        raise RRFormatError(f"{tag} must identify an LEI or an ISO 17442-compatible "
                            f"identifier, not {kind!r}")
    if kind == "ISO_17442_COMPATIBLE":
        flags.add(FLAG_PROVISIONAL_NODE)
    return _identifier(_text(node_groups, "NodeID"), f"{tag}/NodeID", flags), kind


def record_fields(node, flags=None):
    """
    The canonical record, hash version 1: exactly the evidence an
    observation stores, with None for anything the record does not
    state. Leaf text is stripped of surrounding XML whitespace; inner text
    and the order of periods, qualifiers and quantifiers are kept. Dates
    are UTC at millisecond precision; period dates also keep their
    verbatim text. Stated values such as "NULL" stay as stated.
    """

    flags = set() if flags is None else flags
    groups = _shape(node)
    relationship = _shape(groups["Relationship"][0])
    registration = _shape(groups["Registration"][0])

    child, start_type = _node(relationship, "StartNode", flags)
    parent, end_type = _node(relationship, "EndNode", flags)
    if child == parent:
        raise RRFormatError("A relationship cannot connect an LEI to itself")

    periods = None
    if "RelationshipPeriods" in relationship:
        periods = []
        for item in _shape(relationship["RelationshipPeriods"][0])["RelationshipPeriod"]:
            values = _shape(item)
            start = _text(values, "StartDate", required=False)
            end = _text(values, "EndDate", required=False)
            start_utc = parse_datetime(start, "StartDate") if start else None
            end_utc = parse_datetime(end, "EndDate") if end else None
            if start_utc and end_utc and end_utc < start_utc:
                raise RRFormatError("A period ends before it starts")
            periods.append({"period_type": _text(values, "PeriodType", PERIOD_TYPES),
                            "start_text": start, "start_at_utc": utc_text(start_utc),
                            "end_text": end, "end_at_utc": utc_text(end_utc)})

    qualifiers = None
    if "RelationshipQualifiers" in relationship:
        qualifiers = []
        for item in _shape(relationship["RelationshipQualifiers"][0]).get(
                "RelationshipQualifier", ()):
            values = _shape(item)
            qualifiers.append({
                "dimension": _text(values, "QualifierDimension", QUALIFIER_DIMENSIONS),
                "category": _text(values, "QualifierCategory", QUALIFIER_CATEGORIES,
                                  required=False)})

    quantifiers = None
    if "RelationshipQuantifiers" in relationship:
        quantifiers = []
        for item in _shape(relationship["RelationshipQuantifiers"][0]).get(
                "RelationshipQuantifier", ()):
            values = _shape(item)
            amount = _text(values, "QuantifierAmount")
            if not _DECIMAL.match(amount):
                raise RRFormatError(f"QuantifierAmount is not a decimal number: {amount!r}")
            units = _text(values, "QuantifierUnits", QUANTIFIER_UNITS, required=False)
            if units == "PERCENTAGE" and float(amount) > 100:
                flags.add(FLAG_OVER_100_PERCENT)
            quantifiers.append({"measurement_method": _text(values, "MeasurementMethod",
                                                            MEASUREMENT_METHODS),
                                "amount": amount, "units": units})

    dates = {}
    for tag, column, required in (("InitialRegistrationDate", "initial_registration_at_utc", True),
                                  ("LastUpdateDate", "last_update_at_utc", True),
                                  ("NextRenewalDate", "next_renewal_at_utc", False)):
        stated = _text(registration, tag, required=required)
        dates[column] = utc_text(parse_datetime(stated, tag)) if stated else None

    return {
        "child_lei": child,
        "parent_lei": parent,
        "relationship_type": _text(relationship, "RelationshipType", RELATIONSHIP_TYPES),
        "start_node_id_type": start_type,
        "end_node_id_type": end_type,
        "relationship_status": _text(relationship, "RelationshipStatus", RELATIONSHIP_STATUSES),
        "registration_status": _text(registration, "RegistrationStatus", REGISTRATION_STATUSES),
        "validation_sources": _text(registration, "ValidationSources", VALIDATION_SOURCES,
                                    required=False),
        "validation_documents": _text(registration, "ValidationDocuments",
                                      VALIDATION_DOCUMENTS, required=False),
        "validation_reference": _text(registration, "ValidationReference", required=False),
        **dates,
        "managing_lou": _identifier(_text(registration, "ManagingLOU"), "ManagingLOU", flags),
        "periods": periods,
        "qualifiers": qualifiers,
        "quantifiers": quantifiers,
    }


def make_record(ordinal, node):
    flags = set()
    fields = record_fields(node, flags)
    return {"ordinal": ordinal,
            "key": (fields["child_lei"], fields["parent_lei"], fields["relationship_type"]),
            "fields": fields, "record_hash_version": RECORD_HASH_VERSION,
            "record_sha256": record_sha256(fields), "flags": sorted(flags)}


def _identity(path):
    return gleif_xml.identity(path)


# Looked up at call time, so tests may adjust them on this module.
def _block_bytes():
    return BLOCK_BYTES


def _build(ordinal, node):
    return make_record(ordinal, node)


def scan_file(path):
    """First pass: SHA-256, header, every record validated, counted and hashed."""

    return gleif_xml.scan(path, SPEC, _header, _build, _block_bytes)


class RecordStream(gleif_xml.RecordStream):
    """Second pass over a scanned RR file (see gleif_xml.RecordStream)."""

    def __init__(self, path, scan):
        super().__init__(path, scan, SPEC, _header, _build, _block_bytes)
