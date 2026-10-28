
"""
Investigator assessments of how two registered sources relate.

An append-only log (source_relationship_assessments), scoped to one
investigation. Each row assesses a canonical source pair either for
all their claims (pair scope) or for one disclosure (entity, metric,
reporting year, period end). A change of view supersedes the current
row of the same case, pair and scope; nothing is rewritten.

An assessment records an investigator's view and its basis. It is
not proof: INDEPENDENT_ORIGIN means independence has been recorded,
with a traceable evidence reference, a locator, a rationale and
stated limitations, never that it has been established beyond doubt.
"""

import hashlib
import json

from mysql.connector import errors


DEPENDENT = (
    "SAME_DOCUMENT", "REPUBLICATION", "SHARED_PUBLISHER",
    "SAME_CORPORATE_GROUP", "QUOTES_OR_ATTRIBUTES", "SAME_PRIMARY_DISCLOSURE",
)
INDEPENDENT_ORIGIN = "INDEPENDENT_ORIGIN"
UNKNOWN = "UNKNOWN"
RELATIONSHIPS = (*DEPENDENT, INDEPENDENT_ORIGIN, UNKNOWN)
BASES = ("DOCUMENTED", "INVESTIGATOR_JUDGEMENT")

SCOPE_FIELDS = ("entity_name", "metric", "report_year", "period_end")
MAX_TEXT = 5000
TABLE = "source_relationship_assessments"

# Pair evaluation outcomes (reconciliation).
PAIR_DUPLICATE = "APPARENT_DUPLICATE"
PAIR_DEPENDENT = "DEPENDENT"
PAIR_CONFLICT = "CONFLICT"
PAIR_INDEPENDENT = "INDEPENDENT"
PAIR_UNVERIFIED = "UNVERIFIED"


class AssessmentError(ValueError):
    """The assessment cannot be recorded as requested."""


def _clean(value, label, required=True, limit=MAX_TEXT):
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise AssessmentError(f"{label} is required.")
    if len(text) > limit:
        raise AssessmentError(f"{label} exceeds {limit} characters.")
    return text or None


def disclosure_key(entity_name, metric, report_year, period_end):
    """Normalized disclosure identity, as reconciliation groups claims."""
    return (str(entity_name), str(metric), str(int(report_year)), str(period_end))


def scope_of(row):
    """None for a pair-wide assessment, else its disclosure key."""
    if row.get("metric") is None:
        return None
    return disclosure_key(row["entity_name"], row["metric"], row["report_year"],
                          row["period_end"])


def prepare_assessment(source_a_id, source_b_id, relationship, basis, rationale,
                       recorded_by, limitations=None, evidence_source_id=None,
                       evidence_reference=None, evidence_location=None,
                       disclosure=None, supersedes_assessment_id=None):
    """Validate and canonicalize an assessment without touching the database."""

    a, b = int(source_a_id), int(source_b_id)
    if a == b:
        raise AssessmentError("An assessment relates two different sources.")
    a, b = min(a, b), max(a, b)

    if relationship not in RELATIONSHIPS:
        raise AssessmentError(f"Unknown relationship {relationship!r}.")
    if basis not in BASES:
        raise AssessmentError(f"Unknown basis {basis!r}.")

    record = {
        "source_a_id": a, "source_b_id": b,
        "relationship": relationship, "basis": basis,
        "rationale": _clean(rationale, "Rationale"),
        "limitations": _clean(limitations, "Limitations", required=False),
        "recorded_by": _clean(recorded_by, "Reviewer", limit=255),
        "evidence_source_id": None if evidence_source_id is None else int(evidence_source_id),
        "evidence_reference": _clean(evidence_reference, "Evidence reference",
                                     required=False, limit=2048),
        "evidence_location": _clean(evidence_location, "Evidence location",
                                    required=False, limit=500),
        "supersedes_assessment_id": (None if supersedes_assessment_id is None
                                     else int(supersedes_assessment_id)),
    }

    if disclosure is None:
        record.update(dict.fromkeys(SCOPE_FIELDS))
    else:
        missing = [f for f in SCOPE_FIELDS if disclosure.get(f) in (None, "")]
        if missing:
            raise AssessmentError("A disclosure scope needs entity, metric, year and period end.")
        record.update({
            "entity_name": str(disclosure["entity_name"]),
            "metric": str(disclosure["metric"]),
            "report_year": int(disclosure["report_year"]),
            "period_end": str(disclosure["period_end"]),
        })

    if relationship == INDEPENDENT_ORIGIN:
        if record["evidence_source_id"] is None and record["evidence_reference"] is None:
            raise AssessmentError(
                "Independence needs a registered evidence source or an external "
                "evidence reference; otherwise record UNKNOWN.")
        if record["evidence_location"] is None:
            raise AssessmentError("Independence needs the evidence location.")
        if record["limitations"] is None:
            raise AssessmentError("Independence needs its known limitations stated.")

    record["submission_key"] = hashlib.sha256(json.dumps(
        [record[k] for k in sorted(record)], default=str, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")).hexdigest()
    return record


def record_assessment(connection, case_id, **fields):
    """
    Append one assessment in its own transaction.

    Supersession is explicit: if the case, pair and scope already have a
    current assessment, the new one must name it in
    supersedes_assessment_id. An identical resubmission returns the
    existing assessment. The caller owns the connection, which must not
    have a transaction open.
    """

    if getattr(connection, "in_transaction", False) is True:
        raise AssessmentError("The connection already has an open transaction.")

    record = prepare_assessment(**fields)
    cursor = connection.cursor(dictionary=True)

    try:
        connection.start_transaction()

        # Lock both registrations, in ID order; both must be this case's.
        cursor.execute(
            "SELECT source_id FROM case_sources WHERE case_id = %s "
            "AND source_id IN (%s, %s) ORDER BY source_id FOR UPDATE",
            (case_id, record["source_a_id"], record["source_b_id"]))
        if len(cursor.fetchall()) != 2:
            raise AssessmentError("Both sources must be registered in this investigation.")

        if record["evidence_source_id"] is not None:
            cursor.execute(
                "SELECT source_id FROM case_sources WHERE source_id = %s AND case_id = %s",
                (record["evidence_source_id"], case_id))
            if cursor.fetchone() is None:
                raise AssessmentError("The evidence source is not part of this investigation.")

        if record["metric"] is not None:
            cursor.execute(
                # Exact comparison: a scope applies only to identical
                # disclosure text, as in scope_key and reconciliation.
                "SELECT 1 FROM disclosure_claims WHERE source_id IN (%s, %s) "
                "AND entity_name COLLATE utf8mb4_0900_bin = %s "
                "AND metric COLLATE utf8mb4_0900_bin = %s AND report_year = %s "
                "AND period_end = %s LIMIT 1",
                (record["source_a_id"], record["source_b_id"], record["entity_name"],
                 record["metric"], record["report_year"], record["period_end"]))
            if cursor.fetchone() is None:
                raise AssessmentError("Neither source has a claim for that disclosure.")

        cursor.execute(
            f"SELECT assessment_id FROM {TABLE} WHERE submission_key = %s",
            (record["submission_key"],))
        existing = cursor.fetchone()
        if existing:
            connection.rollback()
            return {"assessment_id": int(existing["assessment_id"]), "created": False}

        cursor.execute(
            f"SELECT a.assessment_id FROM {TABLE} a WHERE a.case_id = %s "
            "AND a.source_a_id = %s AND a.source_b_id = %s "
            "AND (a.entity_name COLLATE utf8mb4_0900_bin) <=> %s "
            "AND (a.metric COLLATE utf8mb4_0900_bin) <=> %s AND a.report_year <=> %s "
            "AND a.period_end <=> %s "
            f"AND NOT EXISTS (SELECT 1 FROM {TABLE} s "
            "WHERE s.supersedes_assessment_id = a.assessment_id) FOR UPDATE",
            (case_id, record["source_a_id"], record["source_b_id"],
             *(record[f] for f in SCOPE_FIELDS)))
        current = [int(r["assessment_id"]) for r in cursor.fetchall()]

        if current and record["supersedes_assessment_id"] != current[0]:
            raise AssessmentError(
                f"Assessment {current[0]} is current for this pair and scope; "
                "supersede it explicitly.")
        if not current and record["supersedes_assessment_id"] is not None:
            raise AssessmentError(
                "The superseded assessment is not the current one for this case, "
                "pair and scope.")

        columns = ["case_id", *[k for k in record]]
        cursor.execute(
            f"INSERT INTO {TABLE} ({', '.join(columns)}) "
            f"VALUES ({', '.join(['%s'] * len(columns))})",
            (case_id, *record.values()))
        assessment_id = cursor.lastrowid
        connection.commit()
        return {"assessment_id": assessment_id, "created": True}

    except errors.Error as error:
        connection.rollback()
        if error.errno == 1146:
            raise RuntimeError(
                "Source-relationship assessments (Milestone 23B.1) are not installed. "
                "Nothing was saved.") from None
        if error.errno in (1062, 1644, 1452, 3819):
            raise AssessmentError(
                "The database rejected the assessment (concurrent change, "
                "supersession, scope or evidence rule). Nothing was saved.") from None
        raise
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def current_assessments(rows):
    """Rows that no other row supersedes (pure)."""
    superseded = {int(r["supersedes_assessment_id"]) for r in rows
                  if r.get("supersedes_assessment_id") is not None}
    return [r for r in rows if int(r["assessment_id"]) not in superseded]


def evaluate_pair(assessments, disclosure, automatic=None):
    """
    Outcome for one source pair and one disclosure (pure).

    assessments: the pair's current assessments (any scope). Pair-wide
    dependence applies to every disclosure; pair-wide independence does
    not: a broad view that two sources are independent does not show
    that a particular figure was independently sourced, so only a
    disclosure-scoped INDEPENDENT_ORIGIN can qualify. automatic: a
    document-identity or shared-publisher block found by
    source_identity ("APPARENT_DUPLICATE" or "SHARED_PUBLISHER").

    Order: collect, then automatic blocks, recorded dependence, recorded
    independence, contradictions, and finally unknown or absent evidence.
    Returns outcome plus the IDs and rationales that decided it.
    """

    relevant = [a for a in assessments if scope_of(a) in (None, disclosure)]
    dependence = [a for a in relevant if a["relationship"] in DEPENDENT]
    independence = [a for a in relevant if a["relationship"] == INDEPENDENT_ORIGIN
                    and scope_of(a) == disclosure]
    broad_independence = [a for a in relevant if a["relationship"] == INDEPENDENT_ORIGIN
                          and scope_of(a) is None]

    def cite(items):
        return [{"assessment_id": int(a["assessment_id"]), "relationship": a["relationship"],
                 "basis": a["basis"], "scope": "PAIR" if scope_of(a) is None else "DISCLOSURE",
                 "rationale": a["rationale"]} for a in items]

    result = {"automatic": automatic, "dependence": cite(dependence),
              "independence": cite(independence),
              "pair_level_independence_not_applied": cite(broad_independence)}

    blocked = automatic is not None or dependence
    if blocked and independence:
        result["outcome"] = PAIR_CONFLICT
    elif automatic == PAIR_DUPLICATE:
        result["outcome"] = PAIR_DUPLICATE
    elif blocked:
        result["outcome"] = PAIR_DEPENDENT
    elif independence:
        result["outcome"] = PAIR_INDEPENDENT
    else:
        result["outcome"] = PAIR_UNVERIFIED
    return result


def fetch_assessments(connection, case_id):
    """
    Every assessment of one investigation, oldest first (read-only), or
    None if the table is not installed. The caller ends the transaction.
    """

    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT COUNT(*) AS n FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (TABLE,))
        if not cursor.fetchone()["n"]:
            return None
        cursor.execute(
            f"SELECT assessment_id, case_id, source_a_id, source_b_id, "
            f"{', '.join(SCOPE_FIELDS)}, relationship, basis, evidence_source_id, "
            "evidence_reference, evidence_location, rationale, limitations, "
            "recorded_by, recorded_at_utc, supersedes_assessment_id "
            f"FROM {TABLE} WHERE case_id = %s ORDER BY assessment_id", (case_id,))
        return cursor.fetchall()
    finally:
        cursor.close()
