
"""
Read-only structural probe for GLEIF XML files (Milestone 23B.2B-3,
Stage 3-0).

    python -m src.gleif_format_probe FILE [--max-records N | --full] [--output REPORT.json]

It reads one local .xml file (or the single .xml member of a .zip,
streamed without extraction) and reports its structure only: root,
namespaces, schemaLocation, header codes, element paths and occurrence
counts, attributes, code values, date-time forms, extensions and what the
installed RR parser would reject. It never connects to a database,
never writes next to the source, never fetches anything and never
resolves DTDs or entities.

Privacy: record values are never printed. Only file-level header values
(ContentDate, FileContent, RecordCount, DeltaStart), values matching a
short code pattern on known code-list fields and attributes, and
aggregate counts appear. LEI-shaped values are always withheld.

By default it inspects a bounded sample (the first 1000 records); the
report says so. --full inspects every record, hashes the XML and checks
the header RecordCount.

The version of a GLEIF schema is not stated in its files: the official
XSDs carry no version attribute and RR-CDF 1.1 and 2.1 (and LEI-CDF 2.1
and 3.x) share their namespaces. The probe reports what the file shows
and what it suggests, never a version it cannot establish.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
from collections import Counter, defaultdict
from xml.parsers import expat

from src.claim_subjects import lei_is_valid

PROBE_VERSION = 1
BLOCK_BYTES = 1 << 20
MAX_DEPTH = 32
MAX_PATHS = 2000
MAX_CODE_VALUES = 200
DEFAULT_SAMPLE = 1000

XML_NS = "http://www.w3.org/XML/1998/namespace"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
RR_NS = "http://www.gleif.org/data/schema/rr/2016"
LEI_NS = "http://www.gleif.org/data/schema/leidata/2016"

FAMILIES = {
    (RR_NS, "RelationshipData"): {
        "family": "RR-CDF", "header": "Header", "records": "RelationshipRecords",
        "record": "RelationshipRecord",
        "versions_sharing_namespace": ["RR-CDF 1.1", "RR-CDF 2.1"]},
    (LEI_NS, "LEIData"): {
        "family": "LEI-CDF", "header": "LEIHeader", "records": "LEIRecords",
        "record": "LEIRecord",
        "versions_sharing_namespace": ["LEI-CDF 2.1", "LEI-CDF 3.0", "LEI-CDF 3.1"]},
}

# Children of each element, from the official XSDs (2021-03-04 RR-CDF 2.1
# and LEI-CDF 3.1), keyed by path inside the header or a record. Paths not
# listed here are reported as UNMODELLED, never assumed valid or invalid.
_HEADER = {"ContentDate", "Originator", "FileContent", "DeltaStart", "RecordCount",
           "NextVersion", "Extension"}
OFFICIAL = {
    "RR-CDF": {
        "Header": _HEADER,
        "RelationshipRecord": {"Relationship", "Registration", "NextVersion", "Extension"},
        "RelationshipRecord/Relationship": {
            "StartNode", "EndNode", "RelationshipType", "RelationshipPeriods",
            "RelationshipStatus", "RelationshipQualifiers", "RelationshipQuantifiers",
            "NextVersion", "Extension"},
        "RelationshipRecord/Relationship/StartNode": {"NodeID", "NodeIDType"},
        "RelationshipRecord/Relationship/EndNode": {"NodeID", "NodeIDType"},
        "RelationshipRecord/Relationship/RelationshipPeriods": {"RelationshipPeriod"},
        "RelationshipRecord/Relationship/RelationshipPeriods/RelationshipPeriod": {
            "StartDate", "EndDate", "PeriodType"},
        "RelationshipRecord/Relationship/RelationshipQualifiers": {"RelationshipQualifier"},
        "RelationshipRecord/Relationship/RelationshipQualifiers/RelationshipQualifier": {
            "QualifierDimension", "QualifierCategory"},
        "RelationshipRecord/Relationship/RelationshipQuantifiers": {"RelationshipQuantifier"},
        "RelationshipRecord/Relationship/RelationshipQuantifiers/RelationshipQuantifier": {
            "MeasurementMethod", "QuantifierAmount", "QuantifierUnits"},
        "RelationshipRecord/Registration": {
            "InitialRegistrationDate", "LastUpdateDate", "RegistrationStatus", "NextRenewalDate",
            "ManagingLOU", "ValidationSources", "ValidationDocuments", "ValidationReference",
            "NextVersion", "Extension"},
    },
    "LEI-CDF": {
        "LEIHeader": _HEADER,
        "LEIRecord": {"LEI", "Entity", "Registration", "NextVersion", "Extension"},
        "LEIRecord/Entity": {
            "LegalName", "OtherEntityNames", "TransliteratedOtherEntityNames", "LegalAddress",
            "HeadquartersAddress", "OtherAddresses", "TransliteratedOtherAddresses",
            "RegistrationAuthority", "LegalJurisdiction", "EntityCategory", "EntitySubCategory",
            "LegalForm", "AssociatedEntity", "EntityStatus", "EntityCreationDate",
            "EntityExpirationDate", "EntityExpirationReason", "SuccessorEntity",
            "LegalEntityEvents", "NextVersion"},
        "LEIRecord/Entity/OtherEntityNames": {"OtherEntityName"},
        "LEIRecord/Entity/TransliteratedOtherEntityNames": {"TransliteratedOtherEntityName"},
        "LEIRecord/Entity/RegistrationAuthority": {
            "RegistrationAuthorityID", "OtherRegistrationAuthorityID",
            "RegistrationAuthorityEntityID"},
        "LEIRecord/Entity/LegalForm": {"EntityLegalFormCode", "OtherLegalForm"},
        "LEIRecord/Entity/AssociatedEntity": {"AssociatedLEI", "AssociatedEntityName"},
        "LEIRecord/Entity/SuccessorEntity": {"SuccessorLEI", "SuccessorEntityName"},
        "LEIRecord/Entity/LegalEntityEvents": {"LegalEntityEvent"},
        "LEIRecord/Entity/LegalEntityEvents/LegalEntityEvent": {
            "LegalEntityEventType", "LegalEntityEventEffectiveDate",
            "LegalEntityEventRecordedDate", "ValidationDocuments", "ValidationReference",
            "AffectedFields"},
        "LEIRecord/Registration": {
            "InitialRegistrationDate", "LastUpdateDate", "RegistrationStatus", "NextRenewalDate",
            "ManagingLOU", "ValidationSources", "ValidationAuthority",
            "OtherValidationAuthorities", "NextVersion"},
    },
}

CODE_ELEMENTS = {
    "FileContent", "RelationshipType", "RelationshipStatus", "NodeIDType", "PeriodType",
    "QualifierDimension", "QualifierCategory", "MeasurementMethod", "QuantifierUnits",
    "RegistrationStatus", "ValidationSources", "ValidationDocuments", "EntityStatus",
    "EntityCategory", "EntitySubCategory", "EntityLegalFormCode", "LegalEntityEventType",
    "RegistrationAuthorityID", "ValidationAuthorityID", "LegalJurisdiction", "Country",
    "EntityExpirationReason",
}
CODE_ATTRIBUTES = {"type", f"{XML_NS} lang", "group_type", "event_status"}
DATETIME_ELEMENTS = {
    "ContentDate", "DeltaStart", "StartDate", "EndDate", "InitialRegistrationDate",
    "LastUpdateDate", "NextRenewalDate", "EntityCreationDate", "EntityExpirationDate",
    "LegalEntityEventEffectiveDate", "LegalEntityEventRecordedDate",
}
LEI_ELEMENTS = {"NodeID", "LEI", "ManagingLOU", "SuccessorLEI", "AssociatedLEI", "Originator"}
HEADER_VALUES = {"ContentDate", "FileContent", "RecordCount", "DeltaStart"}

# Fields the proposed LEI2 observation schema needs (23B.2B-3 design),
# reported as presence counts only.
LEI2_FIELDS = [
    "Entity/LegalName", "Entity/LegalName@xml:lang", "Entity/OtherEntityNames/OtherEntityName",
    "Entity/OtherEntityNames/OtherEntityName@type",
    "Entity/OtherEntityNames/OtherEntityName@xml:lang",
    "Entity/TransliteratedOtherEntityNames/TransliteratedOtherEntityName",
    "Entity/LegalAddress", "Entity/HeadquartersAddress", "Entity/OtherAddresses",
    "Entity/TransliteratedOtherAddresses", "Entity/RegistrationAuthority",
    "Entity/LegalJurisdiction", "Entity/EntityCategory", "Entity/EntitySubCategory",
    "Entity/LegalForm", "Entity/AssociatedEntity", "Entity/EntityStatus",
    "Entity/EntityCreationDate", "Entity/SuccessorEntity", "Entity/LegalEntityEvents",
    "Registration/InitialRegistrationDate", "Registration/LastUpdateDate",
    "Registration/RegistrationStatus", "Registration/NextRenewalDate", "Registration/ManagingLOU",
    "Registration/ValidationSources", "Registration/ValidationAuthority",
    "Registration/OtherValidationAuthorities", "Extension",
]

_CODE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_LEI_SHAPE = re.compile(r"^[0-9A-Z]{18}[0-9]{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(Z|[+-]\d{2}:\d{2})?$")
_DECIMAL = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")


class ProbeError(ValueError):
    """The file cannot be probed safely (unsafe or malformed XML, bad container)."""


def open_source(path):
    """
    (stream, container description). A .zip must hold exactly one .xml
    member, which is streamed from the archive, never extracted to disk.
    """

    if zipfile.is_zipfile(path):
        archive = zipfile.ZipFile(path)
        members = [m for m in archive.infolist() if not m.is_dir()]
        if len(members) != 1 or not members[0].filename.lower().endswith(".xml"):
            archive.close()
            raise ProbeError("A .zip must contain exactly one .xml file")
        member = members[0]
        if member.flag_bits & 0x1:
            archive.close()
            raise ProbeError("Encrypted archives are not supported")
        stream = archive.open(member)
        return stream, {"type": "zip", "member": os.path.basename(member.filename),
                        "compressed_bytes": member.compress_size,
                        "uncompressed_bytes": member.file_size}, archive
    return open(path, "rb"), {"type": "xml"}, None


def _rr_rules():
    """The installed RR parser's rules, read from it (not copied)."""

    from src import gleif_rr

    codes = {"RelationshipType": gleif_rr.RELATIONSHIP_TYPES,
             "RelationshipStatus": gleif_rr.RELATIONSHIP_STATUSES,
             "RegistrationStatus": gleif_rr.REGISTRATION_STATUSES,
             "ValidationSources": gleif_rr.VALIDATION_SOURCES,
             "ValidationDocuments": gleif_rr.VALIDATION_DOCUMENTS,
             "PeriodType": gleif_rr.PERIOD_TYPES,
             "QualifierDimension": gleif_rr.QUALIFIER_DIMENSIONS,
             "MeasurementMethod": gleif_rr.MEASUREMENT_METHODS,
             "QuantifierUnits": gleif_rr.QUANTIFIER_UNITS,
             "FileContent": set(gleif_rr.FILE_CONTENT),
             "NodeIDType": gleif_rr.NODE_ID_TYPES}
    return gleif_rr.STRUCTURE, codes, gleif_rr._DATETIME


def _attribute_name(name):
    ns, _, local = name.rpartition(" ")
    if ns == XML_NS:
        return f"xml:{local}"
    if ns == XSI_NS:
        return f"xsi:{local}"
    return f"{{{ns}}}{local}" if ns else local


def _datetime_form(text):
    if _DATETIME.match(text):
        fraction = re.search(r"\.(\d+)", text)
        zone = "Z" if text.endswith("Z") else ("offset" if re.search(r"[+-]\d{2}:\d{2}$", text)
                                              else "no-zone")
        return f"datetime/{zone}/fraction-digits={len(fraction.group(1)) if fraction else 0}"
    if _DATE.match(text):
        return "date-only"
    return "other"


class _Frame:
    __slots__ = ("tag", "ns", "path", "children", "chars", "text", "capture", "extension")

    def __init__(self, tag, ns, path, capture, extension):
        self.tag, self.ns, self.path = tag, ns, path
        self.children, self.chars, self.text = Counter(), 0, []
        self.capture, self.extension = capture, extension


class Inspector:
    """Aggregates structure from expat callbacks; holds no record values."""

    def __init__(self, max_records, rr_rules=None):
        self.max_records = max_records
        self.rr_rules = rr_rules
        self.declaration = None
        self.namespace_declarations = {}
        self.root = None
        self.family = None
        self.stack = []
        self.records = 0
        self.done = False
        self.record_paths = None
        self.paths = Counter()
        self.records_with = Counter()
        self.max_per_record = Counter()
        self.truncated_paths = 0
        self.attributes = Counter()
        self.codes = defaultdict(Counter)
        self.datetime_forms = defaultdict(Counter)
        self.lei_checks = defaultdict(Counter)
        self.quantifiers = Counter()
        self.text_max = Counter()
        self.extensions = defaultdict(lambda: {"namespaces": Counter(), "elements": Counter()})
        self.foreign = Counter()
        self.not_in_official = Counter()
        self.unmodelled = Counter()
        self.rr_rejections = Counter()
        self.rr_flags = Counter()
        self.header = {}
        self.version_features = Counter()
        self.skipped = 0

    # -- expat callbacks -------------------------------------------------

    def xml_declaration(self, version, encoding, standalone):
        self.declaration = {"version": version, "encoding": encoding,
                            "standalone": {1: "yes", 0: "no"}.get(standalone)}

    def namespace(self, prefix, uri):
        if self.root is None:
            self.namespace_declarations[prefix or "(default)"] = uri

    def start(self, name, attrs):
        if self.done:                 # past the sample: ignore, keep nesting balanced
            self.skipped += 1
            return
        ns, _, tag = name.rpartition(" ")
        depth = len(self.stack)
        if depth >= MAX_DEPTH:
            raise ProbeError("Elements are nested too deeply")
        if depth == 0:
            self._start_root(ns, tag, attrs)
            self.stack.append(_Frame(tag, ns, None, False, None))
            return
        parent = self.stack[-1]
        parent.children[tag] += 1

        if parent.extension is not None:
            # Inside an official Extension: inventory names and namespaces only.
            self.extensions[parent.extension]["namespaces"][ns or "(none)"] += 1
            self.extensions[parent.extension]["elements"][tag] += 1
            self.stack.append(_Frame(tag, ns, None, False, parent.extension))
            return

        path = self._path(parent, tag, depth)
        if depth == 2 and self.family and parent.tag == self.family["records"] \
                and tag == self.family["record"]:
            self._start_record()
        self._count_path(path)
        if ns != self.root["namespace"]:
            self.foreign[path] += 1
        for key, value in attrs.items():
            attr = f"{path}@{_attribute_name(key)}"
            self.attributes[attr] += 1
            if key in CODE_ATTRIBUTES:
                self._code(attr, value)
            if self._rr():
                self.rr_rejections[f"attribute not accepted: {attr}"] += 1
        self._check_official(parent, tag, path)

        capture = (tag in CODE_ELEMENTS or tag in DATETIME_ELEMENTS or tag in LEI_ELEMENTS
                   or tag == "QuantifierAmount" or tag in HEADER_VALUES)
        self.stack.append(_Frame(tag, ns, path, capture, path if tag == "Extension" else None))

    def text(self, data):
        if self.done or not self.stack:
            return
        frame = self.stack[-1]
        frame.chars += len(data)
        if frame.capture and frame.chars <= 200:
            frame.text.append(data)

    def end(self, name):
        if self.skipped:
            self.skipped -= 1
            return
        frame = self.stack.pop()
        if frame.path is None:
            return
        if not frame.children:
            self.text_max[frame.path] = max(self.text_max[frame.path], frame.chars)
            if frame.capture:
                self._value(frame, "".join(frame.text).strip() if frame.chars <= 200 else None)
        if self._rr():
            self._rr_structure(frame)
        if self.family and len(self.stack) == 2 and frame.tag == self.family["record"] \
                and self.stack[-1].tag == self.family["records"]:
            self._end_record()

    # -- helpers ---------------------------------------------------------

    def _rr(self):
        return bool(self.rr_rules and self.family and self.family["family"] == "RR-CDF")

    def _start_root(self, ns, tag, attrs):
        locations = []
        for key in (f"{XSI_NS} schemaLocation", f"{XSI_NS} noNamespaceSchemaLocation"):
            locations += attrs.get(key, "").split()
        self.root = {"namespace": ns or None, "element": tag,
                     "attributes": sorted(_attribute_name(k) for k in attrs),
                     "schema_location": locations[:10]}
        self.family = FAMILIES.get((ns, tag))
        if self.family is None:
            self.done = True          # unrecognized document: report the root only

    def _path(self, parent, tag, depth):
        if depth == 1:
            return tag
        if depth == 2 and parent.tag == self.family["records"] and tag == self.family["record"]:
            return tag
        return f"{parent.path}/{tag}"

    def _start_record(self):
        self.record_paths = Counter()

    def _end_record(self):
        for path, count in self.record_paths.items():
            self.records_with[path] += 1
            self.max_per_record[path] = max(self.max_per_record[path], count)
        self.record_paths = None
        self.records += 1
        if self.max_records and self.records >= self.max_records:
            self.done = True

    def _count_path(self, path):
        if path not in self.paths and len(self.paths) >= MAX_PATHS:
            self.truncated_paths += 1
            return
        self.paths[path] += 1
        if self.record_paths is not None:
            self.record_paths[path] += 1

    def _check_official(self, parent, tag, path):
        family = self.family["family"]
        if parent.path is None:
            allowed = {self.family["header"], self.family["records"]}
        elif parent.tag == self.family["records"] and parent.path == self.family["records"]:
            allowed = {self.family["record"]} | ({"Extension"} if family == "LEI-CDF" else set())
        else:
            allowed = OFFICIAL[family].get(parent.path)
            if allowed is None:
                self.unmodelled[parent.path] += 0          # listed, children not judged
                return
        if tag not in allowed:
            self.not_in_official[path] += 1

    @staticmethod
    def _safe(value):
        if value is None:
            return "<value over 200 characters>"
        if _LEI_SHAPE.match(value):
            return "<LEI-shaped value withheld>"
        return value if _CODE.match(value) else "<non-code value>"

    def _code(self, key, value):
        shown = self._safe(value)
        values = self.codes[key]
        if shown not in values and len(values) >= MAX_CODE_VALUES:
            shown = "<further distinct values not listed>"
        values[shown] += 1

    def _value(self, frame, text):
        tag, path = frame.tag, frame.path
        in_header = path.startswith(self.family["header"] + "/")
        if in_header and tag in HEADER_VALUES:
            self.header[tag] = text if text is None or tag != "RecordCount" or text.isdigit() \
                else "<not a whole number>"
        if tag in LEI_ELEMENTS:
            kind = ("valid ISO 17442 check digits" if text and lei_is_valid(text)
                    else "LEI-shaped, invalid check digits" if text and _LEI_SHAPE.match(text)
                    else "not LEI-shaped")
            self.lei_checks[path][kind] += 1
            if self._rr() and tag in ("NodeID", "ManagingLOU") and kind == "not LEI-shaped":
                self.rr_rejections[f"identifier not in LEI form: {path}"] += 1
            elif self._rr() and tag in ("NodeID", "ManagingLOU") and kind != "valid ISO 17442 check digits":
                self.rr_flags[f"identifier fails ISO 17442 check digits: {path}"] += 1
        elif tag in DATETIME_ELEMENTS:
            form = _datetime_form(text or "")
            self.datetime_forms[path][form] += 1
            if self._rr() and not self._rr_datetime_ok(text):
                self.rr_rejections[f"date-time form not accepted: {path} ({form})"] += 1
        elif tag == "QuantifierAmount":
            self._amount(text)
        elif tag in CODE_ELEMENTS:
            self._code(path, text)
            if self._rr():
                accepted = self.rr_rules[1].get(tag)
                if accepted is not None and text not in accepted:
                    self.rr_rejections[f"value not accepted: {path} = {self._safe(text)}"] += 1
                elif tag == "NodeIDType" and text == "ISO_17442_COMPATIBLE":
                    self.rr_flags[f"provisional node identifier: {path}"] += 1
            self._version_feature(tag, text)

    def _rr_datetime_ok(self, text):
        match = self.rr_rules[2].match(text or "")
        return bool(match) and not (match.group(1) or "")[3:].strip("0")

    def _amount(self, text):
        stats = self.quantifiers
        stats["count"] += 1
        if not text or not _DECIMAL.match(text):
            stats["not a decimal"] += 1
            if self._rr():
                self.rr_rejections["QuantifierAmount not accepted: not a decimal"] += 1
            return
        number = float(text)
        digits = len(text.split(".", 1)[1]) if "." in text else 0
        stats["max fraction digits"] = max(stats["max fraction digits"], digits)
        if number < 0:
            stats["negative"] += 1             # valid in RR-CDF 2.1; kept as stated
        elif number == 0:
            stats["zero"] += 1
        if number > 100:
            stats["over 100"] += 1
            if self._rr():
                self.rr_flags["QuantifierAmount over 100 (flagged when units are "
                              "PERCENTAGE)"] += 1

    def _version_feature(self, tag, value):
        new_rr = {("RelationshipType", "IS_FUND-MANAGED_BY"), ("RelationshipType", "IS_SUBFUND_OF"),
                  ("RelationshipType", "IS_FEEDER_TO"), ("RelationshipStatus", "NULL"),
                  ("QualifierCategory", "GOVERNMENT_ACCOUNTING_STANDARD")}
        new_lei = {("EntityStatus", "NULL"), ("EntityCategory", "GENERAL"),
                   ("EntityCategory", "RESIDENT_GOVERNMENT_ENTITY"),
                   ("EntityCategory", "INTERNATIONAL_ORGANIZATION")}
        if (tag, value) in new_rr | new_lei:
            self.version_features[f"{tag} = {value}"] += 1

    def _rr_structure(self, frame):
        structure = self.rr_rules[0]
        rules = structure.get(frame.tag)
        if rules is None or not frame.path.startswith(("Header", "RelationshipRecord")):
            return
        allowed = {rule[0] for rule in rules}
        for child in frame.children:
            if child not in allowed:
                self.rr_rejections[f"element not accepted: {frame.path}/{child}"] += 1
        for name, low, high in rules:
            count = frame.children.get(name, 0)
            if count < low or (high is not None and count > high):
                self.rr_rejections[f"occurrence not accepted: {frame.path}/{name} x{count} "
                                   f"(installed parser: {low}..{high if high is not None else 'n'})"] += 1

    # -- report ----------------------------------------------------------

    def report(self):
        family = self.family
        detected = {"family": family["family"] if family else None,
                    "namespace_recognized": family is not None,
                    "version_stated_in_file": False}
        if family:
            features = dict(self.version_features)
            if family["family"] == "LEI-CDF":
                for path in ("LEIRecord/Entity/EntityCreationDate",
                             "LEIRecord/Entity/LegalEntityEvents",
                             "LEIRecord/Entity/EntitySubCategory"):
                    if self.records_with[path]:
                        features[f"element {path}"] = self.records_with[path]
                if self.max_per_record["LEIRecord/Entity/SuccessorEntity"] > 1:
                    features["repeated SuccessorEntity"] = self.max_per_record[
                        "LEIRecord/Entity/SuccessorEntity"]
                later = "LEI-CDF 3.x"
            else:
                later = "RR-CDF 2.1"
            documents = sorted({token.rsplit("/", 1)[-1] for token in self.root["schema_location"]
                                if token.lower().endswith(".xsd")})
            detected.update(
                versions_sharing_namespace=family["versions_sharing_namespace"],
                schema_location_documents=documents,
                version_features_observed=features,
                conclusion=(
                    "The schema version is not stated in the file (the official XSDs carry no "
                    "version attribute and these versions share one namespace). "
                    + (f"Observed elements or values first published in {later} suggest {later} "
                       "or later; this is evidence, not proof." if features else
                       "The inspected records show no version-distinguishing features.")
                    + (f" schemaLocation names {', '.join(documents)} (a declaration, not "
                       "validation)." if documents else "")))
        else:
            detected["conclusion"] = "Unrecognized root element or namespace; nothing else inspected."

        paths = {path: {"occurrences": self.paths[path], "records_with": self.records_with[path],
                        "max_per_record": self.max_per_record[path],
                        **({"max_text_chars": self.text_max[path]} if path in self.text_max else {})}
                 for path in sorted(self.paths)}
        report = {
            "xml_declaration": self.declaration,
            "root": self.root,
            "namespace_declarations": self.namespace_declarations,
            "detected": detected,
            "header": {"fields": {p.split("/", 1)[1]: c for p, c in sorted(self.paths.items())
                                  if family and p.startswith(family["header"] + "/")
                                  and p.count("/") == 1},
                       "values": self.header},
            "records_inspected": self.records,
            "paths": paths,
            "truncated_paths": self.truncated_paths,
            "attributes": dict(sorted(self.attributes.items())),
            "code_values": {k: dict(v.most_common()) for k, v in sorted(self.codes.items())},
            "datetime_forms": {k: dict(v) for k, v in sorted(self.datetime_forms.items())},
            "identifier_checks": {k: dict(v) for k, v in sorted(self.lei_checks.items())},
            "quantifier_amounts": dict(self.quantifiers),
            "extensions": {k: {"namespaces": dict(v["namespaces"]),
                               "elements": dict(v["elements"].most_common(50))}
                           for k, v in sorted(self.extensions.items())},
            "elements_outside_document_namespace": dict(self.foreign),
            "not_in_official_structure": dict(self.not_in_official),
            "containers_not_modelled_by_probe": sorted(self.unmodelled),
        }
        if family and family["family"] == "RR-CDF" and self.rr_rules:
            report["installed_rr_parser"] = {
                "would_accept_all_inspected_records": not self.rr_rejections,
                "rejections": dict(self.rr_rejections.most_common()),
                "flags_raised": dict(self.rr_flags.most_common()),
                "note": "Element order is not checked here; the parser also rejects "
                        "out-of-order elements."}
        if family and family["family"] == "LEI-CDF":
            report["proposed_lei2_fields_present"] = {
                field: self.records_with.get("LEIRecord/" + field.split("@")[0], 0)
                if "@" not in field else
                self.attributes.get("LEIRecord/" + field, 0)
                for field in LEI2_FIELDS}
        return report


def _hardened_parser(inspector):
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    def refuse(*_):
        raise ProbeError("DTDs and entity declarations are not accepted")

    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.UnparsedEntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    parser.SkippedEntityHandler = refuse
    parser.XmlDeclHandler = inspector.xml_declaration
    parser.StartNamespaceDeclHandler = inspector.namespace
    parser.StartElementHandler = inspector.start
    parser.EndElementHandler = inspector.end
    parser.CharacterDataHandler = inspector.text
    return parser


def probe(path, max_records=DEFAULT_SAMPLE, full=False):
    """Inspect one local file; returns the structural report (a dict)."""

    if not full and (not isinstance(max_records, int) or max_records < 1):
        raise ValueError("max_records must be a positive integer")
    path = os.fspath(path)
    stream, container, archive = open_source(path)
    inspector = Inspector(None if full else max_records, rr_rules=_rr_rules())
    parser = _hardened_parser(inspector)
    hasher = hashlib.sha256() if full else None
    bytes_read, reached_end = 0, False
    try:
        while True:
            block = stream.read(BLOCK_BYTES)
            bytes_read += len(block)
            if hasher is not None:
                hasher.update(block)
            try:
                parser.Parse(block, not block)
            except expat.ExpatError as error:
                raise ProbeError(f"Malformed XML: {error}") from None
            if not block:
                reached_end = True
                break
            if inspector.done:
                break
    finally:
        stream.close()
        if archive is not None:
            archive.close()

    report = {"probe_version": PROBE_VERSION,
              "mode": "FULL" if full else f"SAMPLE: up to the first {max_records} records",
              "source": {"file_name": os.path.basename(path), "size_bytes": os.path.getsize(path),
                         "container": container, "xml_bytes_read": bytes_read,
                         "read_to_end": reached_end},
              **inspector.report()}
    if full:
        declared = report["header"]["values"].get("RecordCount")
        report["full_file"] = {
            "xml_sha256": hasher.hexdigest(),
            "header_record_count": declared,
            "records_counted": inspector.records,
            "record_count_matches": str(inspector.records) == str(declared)}
    report["privacy"] = ("Structure and aggregate counts only. Record values appear only as "
                         "code-list values matching a short code pattern; LEI-shaped values and "
                         "other text are withheld.")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only structural probe of a local GLEIF "
                                                 "XML file (no database, no network).")
    parser.add_argument("path")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--max-records", type=int, default=DEFAULT_SAMPLE)
    group.add_argument("--full", action="store_true", help="inspect every record")
    parser.add_argument("--output", help="write the JSON report to this new file")
    args = parser.parse_args(argv)

    report = probe(args.path, max_records=args.max_records, full=args.full)
    text = json.dumps(report, indent=2, sort_keys=False)
    if args.output:
        source_dir = os.path.dirname(os.path.abspath(args.path))
        if os.path.dirname(os.path.abspath(args.output)) == source_dir:
            parser.error("--output must not be written next to the source file")
        with open(args.output, "x", encoding="utf-8") as handle:   # never overwrites
            handle.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
