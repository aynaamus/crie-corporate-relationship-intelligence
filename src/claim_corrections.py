
"""
Claim corrections as an append-only lineage.

A claim is never edited. A correction inserts a replacement claim and
a claim_corrections row linking it to the claim it supersedes, with
the reason and the source evidence for the transition. Restoring an
earlier value (A -> B -> A) is a new RESTORATION event that creates a
new claim carrying the earlier value; the historical claim is never
reactivated.

Fingerprints: original (imported) claims keep the importer's
fingerprint of their own fields. A replacement's fingerprint also
includes the claim it supersedes, so every step of a lineage has a
distinct, deterministic fingerprint even when a value repeats.
"""

import hashlib
import json

from mysql.connector import errors

from src.disclosure_conflicts import normalize_claim


CORRECTION = "CORRECTION"
RESTORATION = "RESTORATION"
KINDS = (CORRECTION, RESTORATION)

MAX_LINEAGE_DEPTH = 1000
MAX_FIELD = 500
MAX_REASON = 5000

# MySQL errors that mean the 22.4A lineage columns are missing.
SCHEMA_ERRNOS = {1054}
DUPLICATE_KEY = 1062


class LineageError(ValueError):
    """The requested transition would break the correction lineage."""


def _sha256_json(data):
    return hashlib.sha256(
        json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def claim_fingerprint(claim, value, location, supersedes=None):
    """
    Fingerprint of a claim with the given value and location.

    Without supersedes this is the importer's format, so original
    claims keep their fingerprints. A replacement passes the claim_id
    it supersedes, which makes each lineage step distinct.
    """

    data = [
        int(claim["source_id"]),
        claim["entity_name"],
        claim["metric"],
        int(claim["report_year"]),
        str(claim["period_end"]),
        value,
        location,
    ]

    if supersedes is not None:
        data.append({"supersedes": int(supersedes)})

    return _sha256_json(data)


def _clean(value, label, required=True, limit=MAX_FIELD):
    text = "" if value is None else str(value).strip()

    if required and not text:
        raise ValueError(f"{label} is required.")

    if len(text) > limit:
        raise ValueError(f"{label} exceeds {limit} characters.")

    return text


def submission_key(original_claim_id, kind, restored_claim_id, value,
                   location, reason, evidence_source_id, evidence_location):
    """Identity of a submitted correction, for duplicate detection."""

    return _sha256_json([
        int(original_claim_id),
        kind,
        None if restored_claim_id is None else int(restored_claim_id),
        str(value).strip(),
        str(location).strip(),
        str(reason).strip(),
        None if evidence_source_id is None else int(evidence_source_id),
        None if evidence_location is None else str(evidence_location).strip(),
    ])


def prepare_replacement(
    original,
    new_value,
    new_location,
    reason,
    evidence_source_id=None,
    evidence_location=None,
):
    """
    Validate a proposed correction without
    changing the database.

    Supporting evidence defaults to the claim's own source at the
    new location (a transcription correction); a different source
    or location may be cited explicitly. Either way the evidence is
    recorded as attribution, not as verification of the new value.
    """

    value = _clean(new_value, "Corrected value")
    location = _clean(new_location, "Source location")
    explanation = _clean(reason, "Correction reason", limit=MAX_REASON)

    if evidence_location is None:
        evidence_at = location
    else:
        evidence_at = _clean(evidence_location, "Supporting evidence location")

    evidence_source = (
        int(original["source_id"]) if evidence_source_id is None
        else int(evidence_source_id)
    )

    if (
        value == str(original["value_text"]).strip()
        and location == str(original["source_location"]).strip()
    ):
        raise ValueError(
            "The corrected claim is identical to the original."
        )

    # Reuse our tested numerical/text normalization.
    normalized = normalize_claim(
        original["metric"],
        value,
    )

    return {
        "source_id": int(original["source_id"]),
        "entity_name": original["entity_name"],
        "metric": original["metric"],
        "report_year": int(original["report_year"]),
        "period_end": original["period_end"],
        "value_text": value,
        "normalized_value": normalized,
        "source_location": location,
        "claim_fingerprint": claim_fingerprint(
            original, value, location, supersedes=original["claim_id"]
        ),
        "correction_reason": explanation,
        "evidence_source_id": evidence_source,
        "evidence_location": evidence_at,
    }


def _ancestors(cursor, claim_id):
    """Claim IDs superseded on the way to claim_id, nearest first."""

    chain, seen, current = [], {claim_id}, claim_id

    for _ in range(MAX_LINEAGE_DEPTH):
        cursor.execute(
            """
            SELECT original_claim_id
            FROM claim_corrections
            WHERE replacement_claim_id = %s
            FOR SHARE
            """,
            (current,),
        )
        row = cursor.fetchone()

        if row is None:
            return chain

        current = int(row["original_claim_id"])

        if current in seen:
            raise LineageError("The correction lineage contains a cycle.")

        seen.add(current)
        chain.append(current)

    raise LineageError("The correction lineage is too deep to verify.")


def _apply(connection, case_id, original_claim_id, kind, restored_claim_id,
           new_value, new_location, reason, evidence_source_id,
           evidence_location):
    """
    One correction or restoration in its own transaction.

    The caller owns and closes the connection, which must not have a
    transaction open. Everything is written together or not at all.
    """

    # Refuse before any query, so a caller's transaction is never
    # committed or rolled back here.
    if getattr(connection, "in_transaction", False) is True:
        raise ValueError(
            "The connection already has an open transaction; "
            "corrections run in their own transaction."
        )

    key = submission_key(
        original_claim_id, kind, restored_claim_id,
        "" if kind == RESTORATION else new_value,
        "" if kind == RESTORATION else new_location,
        reason, evidence_source_id, evidence_location,
    )

    cursor = connection.cursor(dictionary=True)

    try:
        connection.start_transaction()

        # Lock the claim being superseded; concurrent corrections of
        # the same claim queue here and then see each other's result.
        cursor.execute(
            """
            SELECT
                c.claim_id,
                c.source_id,
                c.entity_name,
                c.metric,
                c.report_year,
                c.period_end,
                c.value_text,
                c.source_location

            FROM disclosure_claims c

            JOIN case_sources s
                ON s.source_id = c.source_id

            WHERE c.claim_id = %s
              AND s.case_id = %s

            FOR UPDATE
            """,
            (original_claim_id, case_id),
        )

        original = cursor.fetchone()

        if original is None:
            raise ValueError(
                "Claim not found in this investigation case."
            )

        # A superseded claim cannot be corrected again, except that
        # an identical resubmission returns the existing result.
        cursor.execute(
            """
            SELECT replacement_claim_id, submission_key
            FROM claim_corrections
            WHERE original_claim_id = %s
            FOR UPDATE
            """,
            (original_claim_id,),
        )

        previous_correction = cursor.fetchone()

        if previous_correction:
            if previous_correction.get("submission_key") == key:
                connection.rollback()
                return {
                    "original_claim_id": original_claim_id,
                    "replacement_claim_id": int(
                        previous_correction["replacement_claim_id"]
                    ),
                }

            raise LineageError(
                "This claim has already been superseded. "
                "Select its current replacement instead."
            )

        if kind == RESTORATION:
            if int(restored_claim_id) not in _ancestors(cursor, original_claim_id):
                raise LineageError(
                    "Only an earlier version of this claim can be restored."
                )

            cursor.execute(
                """
                SELECT value_text, source_location
                FROM disclosure_claims
                WHERE claim_id = %s
                """,
                (restored_claim_id,),
            )
            restored = cursor.fetchone()
            new_value = restored["value_text"]
            new_location = restored["source_location"]

        if (
            evidence_source_id is not None
            and int(evidence_source_id) != int(original["source_id"])
        ):
            cursor.execute(
                """
                SELECT source_id
                FROM case_sources
                WHERE source_id = %s AND case_id = %s
                FOR SHARE
                """,
                (evidence_source_id, case_id),
            )

            if cursor.fetchone() is None:
                raise ValueError(
                    "The supporting evidence source is not part of "
                    "this investigation case."
                )

        replacement = prepare_replacement(
            original,
            new_value,
            new_location,
            reason,
            evidence_source_id,
            evidence_location,
        )

        # Prevent the same fingerprint from being reused.
        cursor.execute(
            """
            SELECT claim_id
            FROM disclosure_claims
            WHERE claim_fingerprint = %s
            """,
            (replacement["claim_fingerprint"],),
        )

        if cursor.fetchone():
            raise ValueError(
                "An identical claim record already exists. "
                "No correction was saved."
            )

        # Insert the corrected version.
        cursor.execute(
            """
            INSERT INTO disclosure_claims (
                source_id,
                entity_name,
                metric,
                report_year,
                period_end,
                value_text,
                normalized_value,
                source_location,
                claim_fingerprint
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s
            )
            """,
            (
                replacement["source_id"],
                replacement["entity_name"],
                replacement["metric"],
                replacement["report_year"],
                replacement["period_end"],
                replacement["value_text"],
                replacement["normalized_value"],
                replacement["source_location"],
                replacement["claim_fingerprint"],
            ),
        )

        replacement_id = cursor.lastrowid

        # Link the original and replacement claims.
        cursor.execute(
            """
            INSERT INTO claim_corrections (
                original_claim_id,
                replacement_claim_id,
                correction_kind,
                restored_claim_id,
                correction_reason,
                evidence_source_id,
                evidence_location,
                submission_key
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                original_claim_id,
                replacement_id,
                kind,
                restored_claim_id,
                replacement["correction_reason"],
                replacement["evidence_source_id"],
                replacement["evidence_location"],
                key,
            ),
        )

        # Both records must be saved together.
        connection.commit()

        return {
            "original_claim_id": original_claim_id,
            "replacement_claim_id": replacement_id,
        }

    except errors.Error as error:
        connection.rollback()

        if error.errno in SCHEMA_ERRNOS:
            raise RuntimeError(
                "The claim-correction lineage schema (Milestone 22.4A) "
                "is not installed. No correction was saved."
            ) from None

        if error.errno == DUPLICATE_KEY:
            raise LineageError(
                "A conflicting correction was saved at the same time. "
                "No correction was saved."
            ) from None

        raise

    except Exception:
        connection.rollback()
        raise

    finally:
        cursor.close()


def correct_claim(
    connection,
    case_id,
    original_claim_id,
    new_value,
    new_location,
    reason,
    evidence_source_id=None,
    evidence_location=None,
):
    """
    Create a replacement claim and record its
    relationship to the original.

    The caller owns and closes the connection.
    """

    return _apply(
        connection, case_id, original_claim_id, CORRECTION, None,
        new_value, new_location, reason,
        evidence_source_id, evidence_location,
    )


def restore_claim(
    connection,
    case_id,
    current_claim_id,
    restored_claim_id,
    reason,
    evidence_source_id=None,
    evidence_location=None,
):
    """
    Supersede the active claim with a new claim carrying the value
    and location of an earlier version in the same lineage. The
    earlier claim stays historical; a new RESTORATION event records
    the reason and evidence.

    The caller owns and closes the connection.
    """

    if restored_claim_id is None:
        raise ValueError("Select the earlier version to restore.")

    return _apply(
        connection, case_id, current_claim_id, RESTORATION,
        int(restored_claim_id), None, None, reason,
        evidence_source_id, evidence_location,
    )


# A correction never changes these; any difference is a lineage problem.
IDENTITY_FIELDS = (
    ("source_id", "source"), ("entity_name", "entity"), ("entity", "entity"),
    ("metric", "metric"), ("report_year", "report year"),
    ("period_end", "period end"),
)


def _identity_changes(before, after):
    return sorted({
        label for key, label in IDENTITY_FIELDS
        if key in before and key in after
        and str(before[key]) != str(after[key])
    })


def build_lineage(claims, corrections, claim_id):
    """
    Reconstruct the lineage containing claim_id (pure).

    claims: dicts with claim_id, value_text, source_location and the
    comparison fields. corrections: claim_corrections rows. Returns
    the versions from the first claim to the active one, the
    transitions between them, and any problems found: conflicting
    successors, cycles, transitions that run backwards, restorations
    of claims outside the lineage, or restored values that differ
    from the claim they restore.
    """

    by_id = {int(c["claim_id"]): c for c in claims}
    problems = []
    successor, predecessor = {}, {}

    for row in sorted(corrections, key=lambda r: int(r["correction_id"])):
        original = int(row["original_claim_id"])
        replacement = int(row["replacement_claim_id"])

        if original in successor:
            problems.append(
                f"claim {original} has more than one replacement "
                f"(corrections {successor[original]['correction_id']} "
                f"and {row['correction_id']})"
            )
            continue

        if replacement in predecessor:
            problems.append(f"claim {replacement} replaces more than one claim")
            continue

        if replacement <= original:
            problems.append(
                f"correction {row['correction_id']} runs backwards "
                f"({original} -> {replacement})"
            )

        if original in by_id and replacement in by_id:
            changed = _identity_changes(by_id[original], by_id[replacement])
            if changed:
                problems.append(
                    f"correction {row['correction_id']} changes the claim's "
                    f"{', '.join(changed)}"
                )

        successor[original] = row
        predecessor[replacement] = row

    if int(claim_id) not in by_id:
        raise ValueError(f"Claim {claim_id} is not in this lineage.")

    # Walk back to the first version.
    root, seen = int(claim_id), {int(claim_id)}
    while root in predecessor:
        root = int(predecessor[root]["original_claim_id"])
        if root in seen:
            problems.append("the correction lineage contains a cycle")
            break
        seen.add(root)

    # Walk forward to the active version.
    versions, transitions, seen = [root], [], {root}
    current = root
    while current in successor and len(versions) <= MAX_LINEAGE_DEPTH:
        row = successor[current]
        current = int(row["replacement_claim_id"])
        if current in seen:
            problems.append("the correction lineage contains a cycle")
            break
        seen.add(current)
        versions.append(current)
        transitions.append(row)

    for row in transitions:
        if row.get("correction_kind") != RESTORATION:
            continue

        restored = row.get("restored_claim_id")
        position = versions.index(int(row["original_claim_id"]))

        if restored is None or int(restored) not in versions[:position]:
            problems.append(
                f"correction {row['correction_id']} restores a claim "
                "outside the earlier lineage"
            )
            continue

        before = by_id.get(int(restored))
        after = by_id.get(int(row["replacement_claim_id"]))
        if before and after and (
            before["value_text"] != after["value_text"]
            or before["source_location"] != after["source_location"]
        ):
            problems.append(
                f"correction {row['correction_id']} does not restore "
                f"the value of claim {restored}"
            )

    missing = [v for v in versions if v not in by_id]
    if missing:
        problems.append(f"lineage references missing claims {missing}")

    return {
        "root_claim_id": root,
        "active_claim_id": versions[-1],
        "versions": [by_id[v] for v in versions if v in by_id],
        "transitions": transitions,
        "problems": problems,
    }


def fetch_lineage(connection, case_id, claim_id):
    """
    Read the full lineage of a claim (read-only).

    Follows explicit claim_corrections links from claim_id in both
    directions, so the history is complete even if a correction
    broke the rule that versions share source, entity, metric and
    period (build_lineage reports that as a problem). Only this
    case's claims, corrections and evidence sources are read; a link
    to a claim outside the case shows as a missing claim. The
    caller owns the connection and ends its transaction.
    """

    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT c.claim_id
            FROM disclosure_claims c
            JOIN case_sources s ON s.source_id = c.source_id
            WHERE c.claim_id = %s AND s.case_id = %s
            """,
            (claim_id, case_id),
        )

        if cursor.fetchone() is None:
            raise ValueError("Claim not found in this investigation case.")

        ids, frontier, corrections = {int(claim_id)}, {int(claim_id)}, {}

        for _ in range(MAX_LINEAGE_DEPTH):
            if not frontier:
                break

            slots = ", ".join(["%s"] * len(frontier))
            cursor.execute(
                f"""
                SELECT cc.correction_id, cc.original_claim_id,
                       cc.replacement_claim_id, cc.correction_kind,
                       cc.restored_claim_id, cc.correction_reason,
                       cc.evidence_source_id, cc.evidence_location,
                       cc.corrected_at_utc,
                       es.document_title AS evidence_document_title
                FROM claim_corrections cc
                LEFT JOIN case_sources es
                    ON es.source_id = cc.evidence_source_id
                   AND es.case_id = %s
                WHERE (cc.original_claim_id IN ({slots})
                       OR cc.replacement_claim_id IN ({slots}))
                  AND EXISTS (
                      SELECT 1 FROM disclosure_claims c
                      JOIN case_sources s ON s.source_id = c.source_id
                      WHERE s.case_id = %s
                        AND c.claim_id IN (cc.original_claim_id,
                                           cc.replacement_claim_id))
                ORDER BY cc.correction_id
                """,
                (case_id, *frontier, *frontier, case_id),
            )

            linked = set()
            for row in cursor.fetchall():
                corrections[int(row["correction_id"])] = row
                linked |= {int(row["original_claim_id"]),
                           int(row["replacement_claim_id"])}

            frontier = linked - ids
            ids |= linked
        else:
            raise LineageError("The correction lineage is too deep to read.")

        slots = ", ".join(["%s"] * len(ids))
        cursor.execute(
            f"""
            SELECT c.claim_id, c.source_id, c.entity_name, c.metric,
                   c.report_year, c.period_end, c.value_text,
                   c.normalized_value, c.source_location,
                   c.claim_fingerprint, c.created_at
            FROM disclosure_claims c
            JOIN case_sources s ON s.source_id = c.source_id
            WHERE s.case_id = %s AND c.claim_id IN ({slots})
            ORDER BY c.claim_id
            """,
            (case_id, *sorted(ids)),
        )
        claims = cursor.fetchall()
    finally:
        cursor.close()

    return build_lineage(
        claims, [corrections[k] for k in sorted(corrections)], claim_id
    )


LINEAGE_COLUMNS = frozenset({
    "correction_kind", "restored_claim_id", "evidence_source_id",
    "evidence_location", "submission_key",
})


def lineage_installed(connection):
    """
    True if the 22.4A lineage columns exist (read-only). The caller
    owns the connection and ends its transaction.
    """

    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = 'claim_corrections'"
        )
        present = {row[0] for row in cursor.fetchall()}
    finally:
        cursor.close()

    return LINEAGE_COLUMNS <= present


NO_CORRECTIONS = "NO_CORRECTIONS"
NO_PROBLEMS_FOUND = "NO_PROBLEMS_FOUND"
PROBLEMS_FOUND = "PROBLEMS_FOUND"
INCOMPLETE = "INCOMPLETE"
NOT_EVALUATED = "NOT_EVALUATED"

_MISSING = "lineage references missing claims"


def diagnose_lineages(claims, corrections, source_ids=None, current_ids=None):
    """
    Check every correction lineage among the given records (pure).

    claims: every available version (current and superseded), as
    build_lineage() expects. corrections: claim_corrections rows with
    correction_kind, restored_claim_id and evidence_source_id.
    source_ids: the investigation's sources, if evidence sources
    should be checked against them. current_ids: the claims that are
    current; a lineage ending anywhere else is missing a later
    correction, so the assessment is incomplete.

    Status:
      NO_CORRECTIONS     nothing to evaluate
      NO_PROBLEMS_FOUND  every lineage was evaluated; no problem found
      PROBLEMS_FOUND     at least one confirmed lineage problem
      INCOMPLETE         no confirmed problem, but a referenced claim
                         is unavailable, so the assessment is partial
    """

    if not corrections:
        return {
            "status": NO_CORRECTIONS, "lineages": 0, "corrections": 0,
            "attribution_not_recorded": 0, "problems": [],
            "missing_claims": [],
        }

    by_id = {int(c["claim_id"]) for c in claims}
    problems, missing = [], set()

    def add(problem):
        if problem not in problems:
            problems.append(problem)

    for row in sorted(corrections, key=lambda r: int(r["correction_id"])):
        for key in ("original_claim_id", "replacement_claim_id", "restored_claim_id"):
            claim_id = row.get(key)
            if claim_id is not None and int(claim_id) not in by_id:
                missing.add(int(claim_id))

        evidence = row.get("evidence_source_id")
        if source_ids is not None and evidence is not None and int(evidence) not in source_ids:
            add(f"correction {row['correction_id']} cites evidence source "
                f"{evidence}, which is outside this investigation")

    covered, lineages, gaps = set(), 0, []
    for row in sorted(corrections, key=lambda r: int(r["correction_id"])):
        start = int(row["original_claim_id"])
        if start in covered or start not in by_id:
            continue

        result = build_lineage(claims, corrections, start)
        covered.update(int(v["claim_id"]) for v in result["versions"])
        lineages += 1

        for problem in result["problems"]:
            if not problem.startswith(_MISSING):
                add(problem)

        active = result["active_claim_id"]
        if current_ids is not None and active not in current_ids:
            gaps.append(
                f"the lineage from claim {result['root_claim_id']} ends at "
                f"claim {active}, which is not current; a later correction "
                "is not available"
            )

    structural = list(problems)
    for claim_id in sorted(missing):
        add(f"claim {claim_id} is referenced by a correction but is not available")
    for gap in gaps:
        add(gap)

    status = (
        PROBLEMS_FOUND if structural
        else INCOMPLETE if missing or gaps
        else NO_PROBLEMS_FOUND
    )

    return {
        "status": status,
        "lineages": lineages,
        "corrections": len(corrections),
        "attribution_not_recorded": sum(
            1 for row in corrections if row.get("evidence_source_id") is None
        ),
        "problems": problems,
        "missing_claims": sorted(missing),
    }


def fetch_case_diagnostics(connection, case_id):
    """
    Lineage diagnostics across one investigation (read-only).

    Works with or without the 22.4A lineage schema; without it,
    restorations and evidence attribution are reported as not
    recorded. Only this case's claims and sources are read. The
    caller owns the connection and ends its transaction.
    """

    installed = lineage_installed(connection)
    lineage = (
        "cc.correction_kind, cc.restored_claim_id, cc.evidence_source_id"
        if installed else
        "'CORRECTION' AS correction_kind, NULL AS restored_claim_id, "
        "NULL AS evidence_source_id"
    )

    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            f"""
            SELECT cc.correction_id, cc.original_claim_id,
                   cc.replacement_claim_id, {lineage}
            FROM claim_corrections cc
            JOIN disclosure_claims o ON o.claim_id = cc.original_claim_id
            JOIN case_sources s ON s.source_id = o.source_id
            WHERE s.case_id = %s
            ORDER BY cc.correction_id
            """,
            (case_id,),
        )
        corrections = cursor.fetchall()

        cursor.execute(
            """
            SELECT c.claim_id, c.value_text, c.source_location,
                   c.source_id, c.entity_name, c.metric,
                   c.report_year, c.period_end
            FROM disclosure_claims c
            JOIN case_sources s ON s.source_id = c.source_id
            WHERE s.case_id = %s
            """,
            (case_id,),
        )
        claims = cursor.fetchall()

        cursor.execute(
            """
            SELECT c.claim_id
            FROM v_current_disclosure_claims c
            JOIN case_sources s ON s.source_id = c.source_id
            WHERE s.case_id = %s
            """,
            (case_id,),
        )
        current_ids = {int(row["claim_id"]) for row in cursor.fetchall()}

        cursor.execute(
            "SELECT source_id FROM case_sources WHERE case_id = %s",
            (case_id,),
        )
        source_ids = {int(row["source_id"]) for row in cursor.fetchall()}
    finally:
        cursor.close()

    result = diagnose_lineages(claims, corrections, source_ids=source_ids,
                               current_ids=current_ids)
    result["lineage_schema"] = "INSTALLED" if installed else "NOT_INSTALLED"
    return result
