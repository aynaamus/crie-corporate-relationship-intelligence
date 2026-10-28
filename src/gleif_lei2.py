
"""
GLEIF LEI-CDF (LEI2) parser (Milestone 23B.2B-3a), on the shared safe
streaming core in src/gleif_xml.py. Pure: reads a local file; no
database, no network. No importer uses it yet.

Format: the leidata/2016 namespace, which LEI-CDF 2.1 and 3.x share;
files do not state their version. The structure follows the LEI-CDF 3.1
XSD (2021-03-04) and the structures observed in a real sample (Stage 3-0).
Attributes are accepted only where that XSD defines them (xml:lang, type,
group_type, event_status, group_id, group_sequence_no, field_xpath).

Kept exactly as stated: names with their languages and types, addresses,
registration and validation authorities, statuses (a stated "NULL" is a
value, distinct from an absent element), successor entities and legal
entity events in document order, with effective and recorded dates kept
apart. Every date keeps its verbatim text and its UTC value.

Rejected as valid but unsupported: NextVersion, record and records-level
Extension, internal files and query responses. Every enumeration is the
complete list of the 3.1 XSD, except the deprecated EntityExpirationReason,
which is kept only as a stated code and never interpreted.

Flags (never part of the hash, never corrections): an identifier failing
ISO 17442 check digits.
"""

import re

from src import gleif_xml
from src.claim_subjects import lei_is_valid
from src.gleif_observations import RECORD_HASH_VERSION, record_sha256
from src.gleif_xml import XML_LANG, SourceChangedError, utc_text  # noqa: F401

LEI_NAMESPACES = {"http://www.gleif.org/data/schema/leidata/2016":
                  "LEI-CDF (leidata/2016 namespace; version not stated in the file)"}

BLOCK_BYTES = 1 << 20
MAX_DEPTH = 16
MAX_RECORD_ELEMENTS = 2000
MAX_TEXT_CHARS = 8192

FILE_CONTENT = {"GLEIF_FULL_PUBLISHED": "FULL", "LOU_FULL_PUBLISHED": "FULL",
                "GLEIF_DELTA_PUBLISHED": "DELTA", "LOU_DELTA_PUBLISHED": "DELTA"}
UNSUPPORTED_FILE_CONTENT = {
    "QUERY_RESPONSE": "a query response has no defined dataset scope",
}

# Verified against the LEI-CDF 3.1 XSD.
OTHER_NAME_TYPES = {"ALTERNATIVE_LANGUAGE_LEGAL_NAME", "PREVIOUS_LEGAL_NAME",
                    "TRADING_OR_OPERATING_NAME"}
TRANSLITERATED_NAME_TYPES = {"PREFERRED_ASCII_TRANSLITERATED_LEGAL_NAME",
                             "AUTO_ASCII_TRANSLITERATED_LEGAL_NAME"}
ENTITY_CATEGORIES = {"GENERAL", "BRANCH", "FUND", "SOLE_PROPRIETOR",
                     "RESIDENT_GOVERNMENT_ENTITY", "INTERNATIONAL_ORGANIZATION"}
ENTITY_SUB_CATEGORIES = {"CENTRAL_GOVERNMENT", "STATE_GOVERNMENT", "LOCAL_GOVERNMENT",
                         "SOCIAL_SECURITY"}
ENTITY_STATUSES = {"ACTIVE", "INACTIVE", "NULL"}
ASSOCIATED_ENTITY_TYPES = {"FUND_FAMILY"}
REGISTRATION_STATUSES = {"PENDING_VALIDATION", "ISSUED", "DUPLICATE", "LAPSED", "MERGED",
                         "RETIRED", "ANNULLED", "CANCELLED", "TRANSFERRED", "PENDING_TRANSFER",
                         "PENDING_ARCHIVAL"}
VALIDATION_SOURCES = {"PENDING", "ENTITY_SUPPLIED_ONLY", "PARTIALLY_CORROBORATED",
                      "FULLY_CORROBORATED"}
VALIDATION_DOCUMENTS = {"ACCOUNTS_FILING", "REGULATORY_FILING", "SUPPORTING_DOCUMENTS",
                        "CONTRACTS", "OTHER_OFFICIAL_DOCUMENTS"}
ADDRESS_TYPES = {"ALTERNATIVE_LANGUAGE_LEGAL_ADDRESS", "ALTERNATIVE_LANGUAGE_HEADQUARTERS_ADDRESS"}
TRANSLITERATED_ADDRESS_TYPES = {"AUTO_ASCII_TRANSLITERATED_LEGAL_ADDRESS",
                                "AUTO_ASCII_TRANSLITERATED_HEADQUARTERS_ADDRESS",
                                "PREFERRED_ASCII_TRANSLITERATED_LEGAL_ADDRESS",
                                "PREFERRED_ASCII_TRANSLITERATED_HEADQUARTERS_ADDRESS"}
EVENT_GROUP_TYPES = {"REVERSE_TAKEOVER", "STANDALONE", "CHANGE_LEGAL_FORM_AND_NAME",
                     "COMPLEX_CHANGE_LEGAL_FORM"}
EVENT_STATUSES = {"IN_PROGRESS", "WITHDRAWN_CANCELLED", "COMPLETED"}
# The complete LegalEntityEventTypeEnum of the 3.1 XSD (19 values).
EVENT_TYPES = {"CHANGE_LEGAL_NAME", "CHANGE_OTHER_NAMES", "CHANGE_LEGAL_ADDRESS",
               "CHANGE_HQ_ADDRESS", "CHANGE_LEGAL_FORM", "DEMERGER", "SPINOFF", "ABSORPTION",
               "ACQUISITION_BRANCH", "TRANSFORMATION_BRANCH_TO_SUBSIDIARY",
               "TRANSFORMATION_SUBSIDIARY_TO_BRANCH", "TRANSFORMATION_UMBRELLA_TO_STANDALONE",
               "BREAKUP", "MERGERS_AND_ACQUISITIONS", "BANKRUPTCY", "LIQUIDATION",
               "VOLUNTARY_ARRANGEMENT", "INSOLVENCY", "DISSOLUTION"}

FLAG_INVALID_CHECK_DIGITS = "IDENTIFIER_FAILS_ISO_17442_CHECK_DIGITS"

_ADDRESS = [("FirstAddressLine", 1, 1), ("AddressNumber", 0, 1),
            ("AddressNumberWithinBuilding", 0, 1), ("MailRouting", 0, 1),
            ("AdditionalAddressLine", 0, 3), ("City", 1, 1), ("Region", 0, 1),
            ("Country", 1, 1), ("PostalCode", 0, 1)]
_AUTHORITY = lambda prefix: [(f"{prefix}ID", 1, 1), (f"Other{prefix}ID", 0, 1),  # noqa: E731
                             (f"{prefix}EntityID", 0, 1)]

STRUCTURE = {
    "LEIHeader": [("ContentDate", 1, 1), ("Originator", 0, 1), ("FileContent", 1, 1),
                  ("DeltaStart", 0, 1), ("RecordCount", 1, 1), ("Extension", 0, 1)],
    "LEIRecord": [("LEI", 1, 1), ("Entity", 1, 1), ("Registration", 1, 1)],
    "Entity": [("LegalName", 1, 1), ("OtherEntityNames", 0, 1),
               ("TransliteratedOtherEntityNames", 0, 1), ("LegalAddress", 1, 1),
               ("HeadquartersAddress", 1, 1), ("OtherAddresses", 0, 1),
               ("TransliteratedOtherAddresses", 0, 1), ("RegistrationAuthority", 0, 1),
               ("LegalJurisdiction", 0, 1), ("EntityCategory", 0, 1),
               ("EntitySubCategory", 0, 1), ("LegalForm", 0, 1), ("AssociatedEntity", 0, 1),
               ("EntityStatus", 1, 1), ("EntityCreationDate", 0, 1),
               ("EntityExpirationDate", 0, 1), ("EntityExpirationReason", 0, 1),
               ("SuccessorEntity", 0, None), ("LegalEntityEvents", 0, 1)],
    "OtherEntityNames": [("OtherEntityName", 1, None)],
    "TransliteratedOtherEntityNames": [("TransliteratedOtherEntityName", 1, None)],
    "LegalAddress": _ADDRESS, "HeadquartersAddress": _ADDRESS,
    "OtherAddress": _ADDRESS, "TransliteratedOtherAddress": _ADDRESS,
    "OtherAddresses": [("OtherAddress", 1, None)],
    "TransliteratedOtherAddresses": [("TransliteratedOtherAddress", 1, None)],
    "RegistrationAuthority": _AUTHORITY("RegistrationAuthority"),
    "LegalForm": [("EntityLegalFormCode", 1, 1), ("OtherLegalForm", 0, 1)],
    "AssociatedEntity": [("AssociatedLEI", 0, 1), ("AssociatedEntityName", 0, 1)],
    "SuccessorEntity": [("SuccessorLEI", 0, 1), ("SuccessorEntityName", 0, 1)],
    "LegalEntityEvents": [("LegalEntityEvent", 1, None)],
    "LegalEntityEvent": [("LegalEntityEventType", 1, 1), ("LegalEntityEventEffectiveDate", 0, 1),
                         ("LegalEntityEventRecordedDate", 1, 1), ("ValidationDocuments", 1, 1),
                         ("ValidationReference", 0, 1), ("AffectedFields", 0, 1)],
    "AffectedFields": [("AffectedField", 1, None)],
    "Registration": [("InitialRegistrationDate", 1, 1), ("LastUpdateDate", 1, 1),
                     ("RegistrationStatus", 1, 1), ("NextRenewalDate", 1, 1),
                     ("ManagingLOU", 1, 1), ("ValidationSources", 0, 1),
                     ("ValidationAuthority", 0, 1), ("OtherValidationAuthorities", 0, 1)],
    "ValidationAuthority": _AUTHORITY("ValidationAuthority"),
    "OtherValidationAuthorities": [("OtherValidationAuthority", 1, None)],
    "OtherValidationAuthority": _AUTHORITY("ValidationAuthority"),
}
UNSUPPORTED = {
    ("LEIHeader", "NextVersion"): "NextVersion content is not supported",
    ("LEIRecord", "NextVersion"): "NextVersion content is not supported",
    ("LEIRecord", "Extension"): "record-level extensions are not supported",
    ("Entity", "NextVersion"): "NextVersion content is not supported",
    ("Registration", "NextVersion"): "NextVersion content is not supported",
}

_LANG = {XML_LANG}
ATTRIBUTES = {
    "LegalName": _LANG, "OtherEntityName": _LANG | {"type"},
    "TransliteratedOtherEntityName": _LANG | {"type"},
    "LegalAddress": _LANG, "HeadquartersAddress": _LANG,
    "OtherAddress": _LANG | {"type"}, "TransliteratedOtherAddress": _LANG | {"type"},
    "AssociatedEntity": {"type"}, "AssociatedEntityName": _LANG, "SuccessorEntityName": _LANG,
    "LegalEntityEvent": {"group_type", "event_status", "group_id", "group_sequence_no"},
    "AffectedField": {"field_xpath"},
}

_IDENTIFIER = re.compile(r"^[0-9A-Z]{18}[0-9]{2}$")
_CODE = re.compile(r"^[A-Z0-9_]{1,64}$")
_JURISDICTION = re.compile(r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_LEGAL_FORM = re.compile(r"^[A-Z0-9]{4}$")
_LANGUAGE = re.compile(r"^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$")


class LEI2FormatError(gleif_xml.GleifFormatError):
    """The file is malformed, unsafe or outside the supported LEI-CDF format."""


SPEC = gleif_xml.DocumentSpec(
    "LEI", LEI_NAMESPACES, "LEIData", "LEIHeader", "LEIRecords", "LEIRecord", LEI2FormatError,
    attributes=ATTRIBUTES, max_depth=MAX_DEPTH, max_record_elements=MAX_RECORD_ELEMENTS,
    max_text_chars=MAX_TEXT_CHARS)

_error = LEI2FormatError


def _shape(node):
    return gleif_xml.shape(node, STRUCTURE, _error, UNSUPPORTED)


def _text(groups, tag, allowed=None, required=True, pattern=None):
    value = gleif_xml.text(groups, tag, _error, allowed, required)
    if value is not None and pattern is not None and not pattern.match(value):
        raise _error(f"Unsupported {tag}: {value!r}")
    return value


def _date(groups, tag, required=True):
    """A stated date-time as {text, utc}, or None when not stated."""

    stated = _text(groups, tag, required=required)
    if stated is None:
        return None
    return {"text": stated, "utc": utc_text(gleif_xml.parse_datetime(stated, tag, _error))}


def _attr(node, name, allowed=None, required=False, pattern=None):
    value = node.attrs.get(name)
    if value is None:
        if required:
            raise _error(f"{node.tag} needs the {name.rpartition(' ')[2]} attribute")
        return None
    value = value.strip()
    if (allowed is not None and value not in allowed) or (pattern and not pattern.match(value)):
        raise _error(f"Unsupported {node.tag} {name.rpartition(' ')[2]}: {value!r}")
    return value


def _identifier(value, label, flags):
    if not _IDENTIFIER.match(value):
        raise _error(f"{label} is not in LEI form: {value!r}")
    if not lei_is_valid(value):
        flags.add(FLAG_INVALID_CHECK_DIGITS)
    return value


def _header(node):
    groups = _shape(node)
    content_date_text = _text(groups, "ContentDate")
    file_content = _text(groups, "FileContent")
    if file_content in UNSUPPORTED_FILE_CONTENT or file_content.endswith("_INTERNAL"):
        reason = UNSUPPORTED_FILE_CONTENT.get(file_content, "an internal, non-public file")
        raise _error(f"Valid GLEIF structure not supported: FileContent {file_content} ({reason})")
    if file_content not in FILE_CONTENT:
        raise _error(f"Unsupported FileContent: {file_content!r}")
    scope = FILE_CONTENT[file_content]
    delta_start = _text(groups, "DeltaStart", required=False)
    if (scope == "DELTA") != (delta_start is not None):
        raise _error("DeltaStart is required for, and only for, a delta file")
    if delta_start is not None:
        gleif_xml.parse_datetime(delta_start, "DeltaStart", _error)
    count_text = _text(groups, "RecordCount")
    if not count_text.isdigit():
        raise _error(f"RecordCount is not a whole number: {count_text!r}")
    originator = _text(groups, "Originator", required=False)
    if originator is not None and not lei_is_valid(originator):
        raise _error(f"Originator is not a valid LEI: {originator!r}")
    header = {
        "content_date_text": content_date_text,
        "content_date_utc": gleif_xml.parse_datetime(content_date_text, "ContentDate", _error),
        "originator": originator, "file_content_text": file_content,
        "publication_scope": scope, "delta_basis": delta_start,
        "header_record_count": int(count_text),
    }
    if "Extension" in groups:
        extension = gleif_xml.header_extension(groups["Extension"][0], _error)
        extension["source_record_counts_match_header"] = (
            sum(s["record_count"] for s in extension["sources"]) == int(count_text))
        header["header_extension"] = extension
    return header


def _name(node, types=None):
    """A name element: exact text with its stated language (and type)."""

    result = {"text": gleif_xml.value(node, node.tag, _error),
              "lang": _attr(node, XML_LANG, pattern=_LANGUAGE)}
    if types is not None:
        result["type"] = _attr(node, "type", types, required=True)
    return result


def _address(node, types=None):
    groups = _shape(node)
    result = {"lang": _attr(node, XML_LANG, pattern=_LANGUAGE)}
    if types is not None:
        result["type"] = _attr(node, "type", types, required=True)
    result.update({
        "first_address_line": _text(groups, "FirstAddressLine"),
        "address_number": _text(groups, "AddressNumber", required=False),
        "address_number_within_building": _text(groups, "AddressNumberWithinBuilding",
                                                required=False),
        "mail_routing": _text(groups, "MailRouting", required=False),
        "additional_address_lines": [gleif_xml.value(n, n.tag, _error)
                                     for n in groups.get("AdditionalAddressLine", ())],
        "city": _text(groups, "City"),
        "region": _text(groups, "Region", required=False, pattern=_JURISDICTION),
        "country": _text(groups, "Country", pattern=_COUNTRY),
        "postal_code": _text(groups, "PostalCode", required=False),
    })
    return result


def _authority(groups, tag, node=None):
    if node is None:
        if tag not in groups:
            return None
        node = groups[tag][0]
    values = _shape(node)
    prefix = "ValidationAuthority" if tag == "OtherValidationAuthority" else tag
    return {"id": _text(values, f"{prefix}ID", pattern=_CODE),
            "other_id": _text(values, f"Other{prefix}ID", required=False),
            "entity_id": _text(values, f"{prefix}EntityID", required=False)}


def _one_of(node, lei_tag, name_tag, flags, label):
    """Exactly one of an LEI or a name (AssociatedEntity, SuccessorEntity)."""

    groups = _shape(node)
    if (lei_tag in groups) == (name_tag in groups):
        raise _error(f"{label} must state exactly one of {lei_tag} or {name_tag}")
    if lei_tag in groups:
        return {"lei": _identifier(_text(groups, lei_tag), f"{label}/{lei_tag}", flags),
                "name": None}
    return {"lei": None, "name": _name(groups[name_tag][0])}


def _event(node):
    groups = _shape(node)
    sequence = _attr(node, "group_sequence_no", pattern=re.compile(r"^[1-9][0-9]{0,8}$"))
    affected = None
    if "AffectedFields" in groups:
        affected = []
        for field in _shape(groups["AffectedFields"][0])["AffectedField"]:
            if field.children:
                raise _error("AffectedField must be a value, not a structure")
            affected.append({"field_xpath": _attr(field, "field_xpath", required=True),
                             "value": "".join(field.text).strip()})
    return {
        "group_type": _attr(node, "group_type", EVENT_GROUP_TYPES, required=True),
        "event_status": _attr(node, "event_status", EVENT_STATUSES, required=True),
        "group_id": _attr(node, "group_id"),
        "group_sequence_no": int(sequence) if sequence else None,
        "type": _text(groups, "LegalEntityEventType", EVENT_TYPES),
        # Stated by the record; neither is inferred or verified by CRIE.
        "effective": _date(groups, "LegalEntityEventEffectiveDate", required=False),
        "recorded": _date(groups, "LegalEntityEventRecordedDate"),
        "validation_documents": _text(groups, "ValidationDocuments", VALIDATION_DOCUMENTS),
        "validation_reference": _text(groups, "ValidationReference", required=False),
        "affected_fields": affected,
    }


def record_fields(node, flags=None):
    """
    The canonical LEI2 record, hash version 1: exactly the evidence an
    entity observation stores. None means the record does not state it;
    a stated "NULL" stays "NULL". Lists keep document order. Leaf text is
    stripped of surrounding XML whitespace only.
    """

    flags = set() if flags is None else flags
    groups = _shape(node)
    entity = _shape(groups["Entity"][0])
    registration = _shape(groups["Registration"][0])

    def names(container, item, types):
        if container not in entity:
            return None
        return [_name(n, types) for n in _shape(entity[container][0])[item]]

    def addresses(container, item, types):
        if container not in entity:
            return None
        return [_address(n, types) for n in _shape(entity[container][0])[item]]

    legal_form = None
    if "LegalForm" in entity:
        values = _shape(entity["LegalForm"][0])
        legal_form = {"code": _text(values, "EntityLegalFormCode", pattern=_LEGAL_FORM),
                      "other": _text(values, "OtherLegalForm", required=False)}

    associated = None
    if "AssociatedEntity" in entity:
        item = entity["AssociatedEntity"][0]
        associated = {"type": _attr(item, "type", ASSOCIATED_ENTITY_TYPES, required=True),
                      **_one_of(item, "AssociatedLEI", "AssociatedEntityName", flags,
                                "AssociatedEntity")}

    successors = None
    if "SuccessorEntity" in entity:
        successors = [_one_of(n, "SuccessorLEI", "SuccessorEntityName", flags, "SuccessorEntity")
                      for n in entity["SuccessorEntity"]]

    events = None
    if "LegalEntityEvents" in entity:
        events = [_event(n) for n in _shape(entity["LegalEntityEvents"][0])["LegalEntityEvent"]]

    return {
        "lei": _identifier(_text(groups, "LEI"), "LEI", flags),
        "legal_name": _name(entity["LegalName"][0]),
        "other_entity_names": names("OtherEntityNames", "OtherEntityName", OTHER_NAME_TYPES),
        "transliterated_other_entity_names": names(
            "TransliteratedOtherEntityNames", "TransliteratedOtherEntityName",
            TRANSLITERATED_NAME_TYPES),
        "legal_address": _address(entity["LegalAddress"][0]),
        "headquarters_address": _address(entity["HeadquartersAddress"][0]),
        "other_addresses": addresses("OtherAddresses", "OtherAddress", ADDRESS_TYPES),
        "transliterated_other_addresses": addresses("TransliteratedOtherAddresses",
                                                    "TransliteratedOtherAddress",
                                                    TRANSLITERATED_ADDRESS_TYPES),
        "registration_authority": _authority(entity, "RegistrationAuthority"),
        "legal_jurisdiction": _text(entity, "LegalJurisdiction", required=False,
                                    pattern=_JURISDICTION),
        "entity_category": _text(entity, "EntityCategory", ENTITY_CATEGORIES, required=False),
        "entity_sub_category": _text(entity, "EntitySubCategory", ENTITY_SUB_CATEGORIES,
                                     required=False),
        "legal_form": legal_form,
        "associated_entity": associated,
        "entity_status": _text(entity, "EntityStatus", ENTITY_STATUSES),
        "entity_creation": _date(entity, "EntityCreationDate", required=False),
        "entity_expiration": _date(entity, "EntityExpirationDate", required=False),
        # Deprecated field; its value list was not verified: a stated code.
        "entity_expiration_reason": _text(entity, "EntityExpirationReason", required=False,
                                          pattern=_CODE),
        "successor_entities": successors,
        "legal_entity_events": events,
        "registration": {
            "initial_registration": _date(registration, "InitialRegistrationDate"),
            "last_update": _date(registration, "LastUpdateDate"),
            "status": _text(registration, "RegistrationStatus", REGISTRATION_STATUSES),
            "next_renewal": _date(registration, "NextRenewalDate"),
            "managing_lou": _identifier(_text(registration, "ManagingLOU"), "ManagingLOU", flags),
            "validation_sources": _text(registration, "ValidationSources", VALIDATION_SOURCES,
                                        required=False),
            "validation_authority": _authority(registration, "ValidationAuthority"),
            "other_validation_authorities": None if "OtherValidationAuthorities" not in registration
            else [_authority(None, "OtherValidationAuthority", n) for n in
                  _shape(registration["OtherValidationAuthorities"][0])["OtherValidationAuthority"]],
        },
    }


def make_record(ordinal, node):
    flags = set()
    fields = record_fields(node, flags)
    return {"ordinal": ordinal, "key": fields["lei"], "fields": fields,
            "record_hash_version": RECORD_HASH_VERSION, "record_sha256": record_sha256(fields),
            "flags": sorted(flags)}


def _block_bytes():
    return BLOCK_BYTES


def _build(ordinal, node):
    return make_record(ordinal, node)


def scan_file(path):
    """First pass: SHA-256, header, every record validated, counted and hashed."""

    return gleif_xml.scan(path, SPEC, _header, _build, _block_bytes)


class RecordStream(gleif_xml.RecordStream):
    """Second pass over a scanned LEI2 file (see gleif_xml.RecordStream)."""

    def __init__(self, path, scan):
        super().__init__(path, scan, SPEC, _header, _build, _block_bytes)
