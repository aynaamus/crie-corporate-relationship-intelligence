
"""
Shared safe streaming XML core for GLEIF files (Milestone 23B.2B-3a),
used by the RR (src/gleif_rr.py) and LEI2 (src/gleif_lei2.py) parsers.
Pure: reads a local file; no database, no network.

Safety: expat is driven directly in fixed-size blocks. Any DOCTYPE,
entity declaration or external entity is rejected before it can be
expanded or fetched. Only the header and one record are held in memory at
a time, under depth, element and text-size limits. Elements must be in
the document's namespace, except the recognized concatenated-file header
extension; attributes are accepted only where a parser allows them.

Two passes: scan() hashes the exact bytes, validates and counts every
record and keeps each record's canonical hash in a temporary file;
RecordStream then re-reads the file and refuses any record whose hash
differs from the first pass, and verify() hashes the unread remainder.
"""

import datetime
import hashlib
from collections import Counter
import os
import re
import tempfile
from xml.parsers import expat

XML_LANG = "http://www.w3.org/XML/1998/namespace lang"
# GLEIF's concatenated files replace each issuer's header with a central
# header whose Extension lists the issuers' original source files.
CONCATENATED_HEADER_NS = "http://www.gleif.org/concatenated-file/header-extension/2.0"

_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$")


class GleifFormatError(ValueError):
    """The file is malformed, unsafe or outside the supported format."""


class SourceChangedError(RuntimeError):
    """The file read now is not the file that was registered."""


class DocumentSpec:
    """What one GLEIF document type looks like to the reader."""

    def __init__(self, label, namespaces, root, header, records, record, error,
                 attributes=None, max_depth=16, max_record_elements=1000, max_text_chars=8192):
        self.label, self.namespaces, self.root = label, namespaces, root
        self.header, self.records, self.record = header, records, record
        self.error = error
        self.attributes = attributes or {}        # tag -> allowed expat attribute names
        self.max_depth = max_depth
        self.max_record_elements = max_record_elements
        self.max_text_chars = max_text_chars


class Node:
    __slots__ = ("tag", "ns", "attrs", "children", "text")

    def __init__(self, tag, ns, attrs):
        self.tag, self.ns, self.attrs, self.children, self.text = tag, ns, attrs, [], []


def events(stream, sink, hasher, block_bytes, error):
    """Feed the stream to a hardened expat parser; events go to `sink`."""

    parser = expat.ParserCreate(namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    def refuse(*_):
        raise error("DTDs and entity declarations are not accepted")

    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.UnparsedEntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    parser.SkippedEntityHandler = refuse

    def start(name, attrs):
        ns, _, tag = name.rpartition(" ")
        sink.append(("start", ns, tag, attrs))

    parser.StartElementHandler = start
    parser.EndElementHandler = lambda name: sink.append(("end",))
    parser.CharacterDataHandler = lambda data: sink.append(("text", data))

    try:
        while True:
            block = stream.read(block_bytes)
            if hasher is not None:
                hasher.update(block)
            parser.Parse(block, not block)
            yield
            if not block:
                return
    except expat.ExpatError as error_:
        raise error(f"Malformed XML: {error_}") from None


def read(stream, spec, hasher, block_bytes):
    """
    Yield ("header", node) once, then ("record", node) for each record in
    file order. Everything outside the current header or record is
    discarded as soon as it is seen.
    """

    error = spec.error
    sink, stack = [], []          # stack: (tag, in_header_extension) of open elements
    building, namespace = [], None
    elements = 0
    seen = []                     # children of the root, in order

    for _ in events(stream, sink, hasher, block_bytes, error):
        for event in sink:
            if event[0] == "start":
                _, ns, tag, attrs = event
                depth = len(stack)
                # Inside the header's Extension (root/Header/Extension/...).
                in_extension = bool(stack) and (stack[-1][1] or (
                    depth == 3 and stack[1][0] == spec.header and stack[2][0] == "Extension"))
                if depth >= spec.max_depth:
                    raise error("Elements are nested too deeply")
                if depth == 0:
                    if ns not in spec.namespaces:
                        raise error(f"Unsupported {spec.label} namespace: {ns or '(none)'}")
                    if tag != spec.root:
                        raise error(f"Unexpected root element: {tag}")
                    namespace = ns
                elif in_extension:
                    if ns != CONCATENATED_HEADER_NS:
                        raise error(f"Unsupported header extension namespace: {ns or '(none)'}")
                elif ns != namespace:
                    if stack[-1][0] == "Extension":
                        raise error(f"Valid GLEIF structure not supported: {stack[-1][0]} "
                                    f"content outside the header (namespace {ns or '(none)'})")
                    raise error(f"Element {tag} is outside the {spec.label} namespace")
                elif depth == 1:
                    if tag not in (spec.header, spec.records) or tag in seen \
                            or (tag == spec.records and seen != [spec.header]):
                        raise error(f"Unexpected document structure at {tag}")
                    seen.append(tag)
                elif depth == 2 and stack[1][0] == spec.records and tag != spec.record:
                    if tag == "Extension":
                        raise error(f"Valid GLEIF structure not supported: Extension in "
                                    f"{spec.records}")
                    raise error(f"Unexpected element in {spec.records}: {tag}")

                if building or tag == spec.header or (depth == 2 and tag == spec.record):
                    allowed = spec.attributes.get(tag, set())
                    if any(key not in allowed for key in attrs):
                        raise error(f"Unexpected attributes on {tag}")
                    elements += 1
                    if elements > spec.max_record_elements:
                        raise error("A record has too many elements")
                    node = Node(tag, ns, dict(attrs))
                    if building:
                        building[-1].children.append(node)
                    building.append(node)
                stack.append((tag, in_extension))
            elif event[0] == "text":
                if building:
                    node = building[-1]
                    node.text.append(event[1])
                    if sum(map(len, node.text)) > spec.max_text_chars:
                        raise error(f"Text of {node.tag} is too long")
                elif event[1].strip():
                    raise error("Unexpected text outside a record")
            else:
                stack.pop()
                if building:
                    node = building.pop()
                    if not building:
                        elements = 0
                        yield ("header" if node.tag == spec.header else "record"), node
        sink.clear()

    if seen != [spec.header, spec.records]:
        raise error(f"The file has no header or no {spec.records}")


def shape(node, structure, error, unsupported=None):
    """Check a node's children against `structure`; return {tag: [nodes]}."""

    rules = structure.get(node.tag)
    if "".join(node.text).strip():
        raise error(f"Unexpected text in {node.tag}")
    order = [rule[0] for rule in rules]
    position, groups = 0, {}
    for child in node.children:
        if child.tag not in order:
            reason = (unsupported or {}).get((node.tag, child.tag))
            if reason:
                raise error(f"Valid GLEIF structure not supported: {child.tag} in {node.tag} "
                            f"({reason})")
            raise error(f"Unsupported element {child.tag} in {node.tag}")
        index = order.index(child.tag)
        if index < position:
            raise error(f"Element {child.tag} is out of order in {node.tag}")
        position = index
        groups.setdefault(child.tag, []).append(child)
    for tag, low, high in rules:
        count = len(groups.get(tag, ()))
        if count < low or (high is not None and count > high):
            reason = (unsupported or {}).get((node.tag, tag, "repeated"))
            if reason and high is not None and count > high:
                raise error(f"Valid GLEIF structure not supported: {count} {tag} in "
                            f"{node.tag} ({reason})")
            raise error(f"{node.tag} needs {low}..{high or 'n'} {tag}, found {count}")
    return groups


def value(node, tag, error):
    """The text of a leaf element, stripped of surrounding XML whitespace."""

    if node.children:
        raise error(f"{tag} must be a value, not a structure")
    text = "".join(node.text).strip()
    if not text:
        raise error(f"{tag} is empty")
    return text


def text(groups, tag, error, allowed=None, required=True):
    nodes = groups.get(tag)
    if not nodes:
        if required:
            raise error(f"{tag} is required")
        return None
    result = value(nodes[0], tag, error)
    if allowed is not None and result not in allowed:
        raise error(f"Unsupported {tag}: {result!r}")
    return result


def parse_datetime(text_, label, error):
    """An xs:dateTime with an explicit zone, as UTC at millisecond precision."""

    match = _DATETIME.match(text_ or "")
    if not match:
        raise error(f"{label} is not a date-time with a time zone: {text_!r}")
    fraction = match.group(1) or ""
    if fraction[3:].strip("0"):
        raise error(f"{label} is more precise than milliseconds: {text_!r}")
    try:
        result = datetime.datetime.fromisoformat(
            text_[:19] + ("." + fraction[:3].ljust(3, "0") if fraction else "")
            + ("+00:00" if match.group(2) == "Z" else match.group(2)))
    except ValueError:
        raise error(f"{label} is not a valid date-time: {text_!r}") from None
    return result.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def utc_text(result):
    return None if result is None else result.strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{result.microsecond // 1000:03d}Z"


def header_extension(node, error):
    """
    The concatenated-file header extension: the issuers' original source
    files, in document order, each with its ContentDate, Originator and
    RecordCount exactly as stated. Anything else is rejected. These are
    provenance of the file, not corroboration of any record.
    """

    if node.attrs or "".join(node.text).strip() or len(node.children) != 1:
        raise error("The header Extension must hold exactly one Sources element")
    sources = node.children[0]
    if sources.tag != "Sources" or sources.attrs or "".join(sources.text).strip() \
            or not sources.children:
        raise error("Unsupported header extension structure")
    result = []
    for source in sources.children:
        if source.tag != "Source" or source.attrs or "".join(source.text).strip():
            raise error(f"Unsupported header extension element: {source.tag}")
        fields = {}
        for child in source.children:
            if child.tag not in ("ContentDate", "Originator", "RecordCount") \
                    or child.tag in fields or child.attrs:
                raise error(f"Unsupported header extension element: Source/{child.tag}")
            fields[child.tag] = value(child, child.tag, error)
        if set(fields) != {"ContentDate", "Originator", "RecordCount"}:
            raise error("Each header extension Source needs ContentDate, Originator and RecordCount")
        parse_datetime(fields["ContentDate"], "Source/ContentDate", error)
        if not fields["RecordCount"].isdigit():
            raise error("Source/RecordCount is not a whole number")
        result.append({"content_date_text": fields["ContentDate"],
                       "originator": fields["Originator"],
                       "record_count": int(fields["RecordCount"])})
    return {"namespace": CONCATENATED_HEADER_NS, "sources": result}


def identity(path):
    status = os.stat(path)
    return {"size": status.st_size, "mtime_ns": status.st_mtime_ns, "inode": status.st_ino}


def scan(path, spec, parse_header, build_record, block_bytes):
    """
    First pass: SHA-256 of the exact bytes, the header, and every record
    validated and counted, in bounded memory. Each record's canonical
    hash is kept on disk (32 bytes a record) for the second pass.
    """

    before = identity(path)
    hasher, header, count, flags = hashlib.sha256(), None, 0, Counter()
    digests = tempfile.TemporaryFile()
    with open(path, "rb") as stream:
        for kind, node in read(stream, spec, hasher, block_bytes()):
            if kind == "header":
                header = parse_header(node)
            else:
                count += 1
                record = build_record(count, node)
                flags.update(record.get("flags", ()))
                digests.write(bytes.fromhex(record["record_sha256"]))
    if identity(path) != before:
        raise SourceChangedError("The file changed while it was being scanned")
    if header["header_record_count"] != count:
        raise spec.error(f"RecordCount says {header['header_record_count']} records; "
                         f"the file has {count}")
    return {"sha256": hasher.hexdigest(), "file_name": os.path.basename(path),
            "identity": before, "header": header, "record_count": count,
            "format": spec.label, "record_digests": digests, "record_flags": dict(flags)}


class RecordStream:
    """
    Second pass over a scanned file. Iterating yields the records in file
    order, each checked against its first-pass hash before it is handed
    out; stopping early is allowed. verify() hashes whatever was not
    parsed and fails unless bytes, count, header and identity still match.
    """

    def __init__(self, path, scan_result, spec, parse_header, build_record, block_bytes):
        self.path, self.scan = path, scan_result
        self._spec, self._parse_header, self._build = spec, parse_header, build_record
        self._block_bytes = block_bytes
        self._hasher = hashlib.sha256()
        self._stream = open(path, "rb")
        self._records = 0
        self._exhausted = False
        self._digests = scan_result["record_digests"]
        self._digests.seek(0)
        self.check_unchanged()

    def check_unchanged(self):
        if identity(self.path) != self.scan["identity"]:
            raise SourceChangedError("The source file changed after it was registered")

    def __iter__(self):
        for kind, node in read(self._stream, self._spec, self._hasher, self._block_bytes()):
            if kind == "header":
                if self._parse_header(node) != self.scan["header"]:
                    raise SourceChangedError("The file header differs from the registered file")
                continue
            self._records += 1
            if self._records > self.scan["record_count"]:
                raise SourceChangedError("The file has more records than were registered")
            record = self._build(self._records, node)
            if self._digests.read(32) != bytes.fromhex(record["record_sha256"]):
                raise SourceChangedError(f"Record {self._records} differs from the registered file")
            yield record
        self._exhausted = True

    def verify(self):
        """Hash the unread remainder and compare everything with the scan."""

        while True:
            block = self._stream.read(self._block_bytes())
            if not block:
                break
            self._hasher.update(block)
        self.check_unchanged()
        if self._hasher.hexdigest() != self.scan["sha256"]:
            raise SourceChangedError("The file's SHA-256 differs from the registered file")
        if self._exhausted and self._records != self.scan["record_count"]:
            raise SourceChangedError("The file's record count differs from the registered file")

    def close(self):
        self._stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
