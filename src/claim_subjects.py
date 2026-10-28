
"""
Case-scoped identity of disclosure claim subjects (Milestone 23B.2A).

An investigator records what legal entity the exact subject text of a
case's claims refers to: MAPPED to one LEI, AMBIGUOUS between named
candidates, NO_LEI (the entity has none), or UNRESOLVED. Mappings are
an append-only log (claim_subject_mappings); a change of view
supersedes the current mapping of the same case and subject text.

Nothing is assigned from name similarity, and claims are never
changed: the mapping refers to their subject text exactly as written
(case, spaces and Unicode form all matter). A mapping records an
investigator's identification and its evidence; it says nothing about
ownership, control or any relationship between entities.
"""

import hashlib
import json
import re

from mysql.connector import errors


STATUSES = ("MAPPED", "AMBIGUOUS", "NO_LEI", "UNRESOLVED")
NAME_TYPES = ("LEGAL_NAME", "TRADING_NAME", "PREVIOUS_NAME", "ALIAS", "ABBREVIATION", "OTHER")
BASES = ("DOCUMENTED", "INVESTIGATOR_JUDGEMENT")
TABLE = "claim_subject_mappings"
MAX_TEXT = 5000
LEI_PATTERN = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")

# Exact comparison of text whose column collation ignores case or spaces.
EXACT = "COLLATE utf8mb4_0900_bin"

UNMAPPED = "UNMAPPED"


class MappingError(ValueError):
    """The mapping cannot be recorded as requested."""


def lei_is_valid(lei):
    """ISO 17442 form and check digits (ISO 7064 MOD 97-10)."""

    if not isinstance(lei, str) or not LEI_PATTERN.match(lei):
        return False
    digits = "".join(str(int(ch, 36)) for ch in lei)
    return int(digits) % 97 == 1


def _clean(value, label, required=True, limit=MAX_TEXT):
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise MappingError(f"{label} is required.")
    if len(text) > limit:
        raise MappingError(f"{label} exceeds {limit} characters.")
    return text or None


def _lei(value, label):
    if not lei_is_valid(value):
        raise MappingError(f"{label} {value!r} is not a valid LEI (form and check digits).")
    return value


def prepare_mapping(subject_text, status, basis, rationale, recorded_by, lei=None,
                    name_type=None, candidate_leis=None, evidence_source_id=None,
                    evidence_reference=None, evidence_location=None, limitations=None,
                    supersedes_mapping_id=None):
    """Validate a mapping without touching the database."""

    # The subject is kept exactly as the claims write it.
    if not isinstance(subject_text, str) or not subject_text.strip():
        raise MappingError("Subject text is required.")
    if len(subject_text) > 255:
        raise MappingError("Subject text exceeds 255 characters.")
    if status not in STATUSES:
        raise MappingError(f"Unknown status {status!r}.")
    if basis not in BASES:
        raise MappingError(f"Unknown basis {basis!r}.")

    record = {
        "subject_text": subject_text, "status": status, "basis": basis,
        "lei": None, "name_type": None, "candidate_leis": None,
        "evidence_source_id": None if evidence_source_id is None else int(evidence_source_id),
        "evidence_reference": _clean(evidence_reference, "Evidence reference", False, 2048),
        "evidence_location": _clean(evidence_location, "Evidence location", False, 500),
        "rationale": _clean(rationale, "Rationale"),
        "limitations": _clean(limitations, "Limitations", required=False),
        "recorded_by": _clean(recorded_by, "Reviewer", limit=255),
        "supersedes_mapping_id": (None if supersedes_mapping_id is None
                                  else int(supersedes_mapping_id)),
    }

    if status == "MAPPED":
        record["lei"] = _lei(lei, "LEI")
        if name_type not in NAME_TYPES:
            raise MappingError("A confirmed mapping needs the name type of the subject text.")
        record["name_type"] = name_type
        if record["limitations"] is None:
            raise MappingError("A confirmed mapping needs its known limitations stated.")
    elif lei is not None:
        raise MappingError(f"A {status} mapping must not name a single LEI.")

    if status == "AMBIGUOUS":
        candidates = sorted({_lei(c, "Candidate") for c in (candidate_leis or [])})
        if len(candidates) < 2:
            raise MappingError("An ambiguous mapping needs at least two candidate LEIs.")
        record["candidate_leis"] = json.dumps(candidates)
    elif candidate_leis:
        raise MappingError("Only an ambiguous mapping lists candidates.")

    if status in ("MAPPED", "NO_LEI"):
        if record["evidence_source_id"] is None and record["evidence_reference"] is None:
            raise MappingError(
                "This needs a registered evidence source or an authoritative registry "
                "reference; otherwise record UNRESOLVED.")
        if record["evidence_location"] is None:
            raise MappingError("This needs the evidence location.")

    record["submission_key"] = hashlib.sha256(json.dumps(
        [record[k] for k in sorted(record)], default=str, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")).hexdigest()
    return record


def record_mapping(connection, case_id, **fields):
    """
    Append one mapping in its own transaction.

    The subject must appear, exactly, in the case's claims. If the
    subject already has a current mapping, the new one must name it in
    supersedes_mapping_id. An identical resubmission returns the
    existing mapping. The caller owns the connection, which must not
    have a transaction open.
    """

    if getattr(connection, "in_transaction", False) is True:
        raise MappingError("The connection already has an open transaction.")

    record = prepare_mapping(**fields)
    cursor = connection.cursor(dictionary=True)

    try:
        connection.start_transaction()

        # Serialize mapping changes within the case.
        cursor.execute("SELECT case_id FROM investigation_cases WHERE case_id = %s FOR UPDATE",
                       (case_id,))
        if cursor.fetchone() is None:
            raise MappingError("Investigation case not found.")

        cursor.execute(
            "SELECT 1 FROM disclosure_claims c JOIN case_sources s ON s.source_id = c.source_id "
            f"WHERE s.case_id = %s AND c.entity_name {EXACT} = %s LIMIT 1",
            (case_id, record["subject_text"]))
        if cursor.fetchone() is None:
            raise MappingError("No claim of this investigation uses exactly that subject text.")

        if record["evidence_source_id"] is not None:
            cursor.execute(
                "SELECT source_id FROM case_sources WHERE source_id = %s AND case_id = %s",
                (record["evidence_source_id"], case_id))
            if cursor.fetchone() is None:
                raise MappingError("The evidence source is not part of this investigation.")

        leis = [record["lei"]] if record["lei"] else json.loads(record["candidate_leis"] or "[]")
        if leis:
            cursor.execute(
                f"SELECT lei FROM legal_entities WHERE lei IN ({', '.join(['%s'] * len(leis))})",
                tuple(leis))
            known = {row["lei"] for row in cursor.fetchall()}
            unknown = sorted(set(leis) - known)
            if unknown:
                raise MappingError(f"Not a recorded legal entity: {', '.join(unknown)}.")

        cursor.execute(f"SELECT mapping_id FROM {TABLE} WHERE submission_key = %s",
                       (record["submission_key"],))
        existing = cursor.fetchone()
        if existing:
            connection.rollback()
            return {"mapping_id": int(existing["mapping_id"]), "created": False}

        cursor.execute(
            f"SELECT m.mapping_id FROM {TABLE} m WHERE m.case_id = %s "
            f"AND m.subject_text {EXACT} = %s "
            f"AND NOT EXISTS (SELECT 1 FROM {TABLE} s WHERE s.supersedes_mapping_id = m.mapping_id) "
            "FOR UPDATE",
            (case_id, record["subject_text"]))
        current = [int(r["mapping_id"]) for r in cursor.fetchall()]

        if current and record["supersedes_mapping_id"] != current[0]:
            raise MappingError(
                f"Mapping {current[0]} is current for this subject; supersede it explicitly.")
        if not current and record["supersedes_mapping_id"] is not None:
            raise MappingError("The superseded mapping is not the current one for this subject.")

        columns = ["case_id", *record]
        cursor.execute(
            f"INSERT INTO {TABLE} ({', '.join(columns)}) "
            f"VALUES ({', '.join(['%s'] * len(columns))})",
            (case_id, *record.values()))
        mapping_id = cursor.lastrowid
        connection.commit()
        return {"mapping_id": mapping_id, "created": True}

    except errors.Error as error:
        connection.rollback()
        if error.errno == 1146:
            raise RuntimeError(
                "Claim-subject mappings (Milestone 23B.2A) are not installed. "
                "Nothing was saved.") from None
        if error.errno in (1062, 1644, 1452, 3819):
            raise MappingError(
                "The database rejected the mapping (concurrent change, supersession, "
                "LEI or evidence rule). Nothing was saved.") from None
        raise
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def current_mappings(rows):
    """Mappings that no other mapping supersedes (pure)."""
    superseded = {int(r["supersedes_mapping_id"]) for r in rows
                  if r.get("supersedes_mapping_id") is not None}
    return [r for r in rows if int(r["mapping_id"]) not in superseded]


def subject_identities(subjects, mappings, case_parties=()):
    """
    Each subject's current identity and diagnostics (pure).

    subjects: the exact subject texts used by the case's claims.
    mappings: every mapping of the case (current and superseded).
    case_parties: LEIs of the relationship the case examines, if known.

    Diagnostics never change a mapping; they point to what needs
    review: subjects without a mapping, ambiguity, a mapped entity that
    is not a party to the case relationship, an LEI mapped from several
    subject texts, a reassigned identity, or a mapping whose subject no
    longer appears in any claim.
    """

    current = {m["subject_text"]: m for m in current_mappings(mappings)}
    history = {}
    for m in sorted(mappings, key=lambda r: int(r["mapping_id"])):
        history.setdefault(m["subject_text"], []).append(m)
    lei_subjects = {}
    for m in current.values():
        if m["status"] == "MAPPED":
            lei_subjects.setdefault(m["lei"], []).append(m["subject_text"])

    results = []
    for subject in sorted(set(subjects)):
        mapping = current.get(subject)
        notes = []
        if mapping is None:
            status, lei, candidates = UNMAPPED, None, []
            notes.append("No identity has been recorded for this subject.")
        else:
            status, lei = mapping["status"], mapping["lei"]
            raw = mapping.get("candidate_leis")
            candidates = json.loads(raw) if isinstance(raw, str) else list(raw or [])
            if status == "AMBIGUOUS":
                notes.append("The subject could refer to more than one legal entity; "
                             "no single identity is used.")
            if status == "MAPPED" and case_parties and lei not in case_parties:
                notes.append("The mapped entity is not a party to the relationship "
                             "this case examines.")
            if status == "MAPPED" and len(lei_subjects[lei]) > 1:
                others = [s for s in lei_subjects[lei] if s != subject]
                notes.append(f"The same LEI is also mapped from: {', '.join(others)}.")
            leis = {m["lei"] for m in history[subject] if m["lei"]}
            if len(history[subject]) > 1 and (len(leis) > 1 or len(
                    {m["status"] for m in history[subject]}) > 1):
                notes.append("The identity of this subject was reassigned; "
                             "earlier mappings remain in the history.")
        results.append({"subject_text": subject, "status": status, "lei": lei,
                        "candidate_leis": candidates,
                        "mapping_id": None if mapping is None else int(mapping["mapping_id"]),
                        "notes": notes})

    orphans = sorted(set(current) - set(subjects))
    return {"subjects": results, "orphaned_mappings": orphans}


def _table_exists(cursor):
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (TABLE,))
    return bool(cursor.fetchone()["n"])


def fetch_mappings(connection, case_id):
    """
    Every mapping of one investigation, oldest first (read-only), or
    None if the table is not installed. The caller ends the transaction.
    """

    cursor = connection.cursor(dictionary=True)
    try:
        if not _table_exists(cursor):
            return None
        cursor.execute(
            "SELECT mapping_id, case_id, subject_text, status, lei, name_type, "
            "candidate_leis, basis, evidence_source_id, evidence_reference, "
            "evidence_location, rationale, limitations, recorded_by, recorded_at_utc, "
            f"supersedes_mapping_id FROM {TABLE} WHERE case_id = %s ORDER BY mapping_id",
            (case_id,))
        return cursor.fetchall()
    finally:
        cursor.close()


def fetch_case_subjects(connection, case_id):
    """Exact subject texts of the case's claims, with claim counts (read-only)."""

    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            f"SELECT c.entity_name {EXACT} AS subject_text, COUNT(*) AS claims "
            "FROM disclosure_claims c JOIN case_sources s ON s.source_id = c.source_id "
            f"WHERE s.case_id = %s GROUP BY c.entity_name {EXACT} ORDER BY 1",
            (case_id,))
        return cursor.fetchall()
    finally:
        cursor.close()


def fetch_case_parties(connection, case_id):
    """LEIs of the relationship the case examines, or () where not recorded."""

    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = 'investigation_cases' AND COLUMN_NAME IN ('child_lei', 'parent_lei')")
        if len(cursor.fetchall()) != 2:
            return ()
        cursor.execute("SELECT child_lei, parent_lei FROM investigation_cases WHERE case_id = %s",
                       (case_id,))
        row = cursor.fetchone()
        return () if row is None else (row["child_lei"], row["parent_lei"])
    finally:
        cursor.close()
