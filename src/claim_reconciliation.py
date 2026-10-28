
"""
Evidence reconciliation for current disclosure claims.

Pure functions: no database access and no file writes.
Input is the DataFrame returned by
load_current_disclosures(strict=False).

Reconciliation never selects a winning claim. Where
claims disagree, every competing value is preserved and
the disclosure is marked UNRESOLVED_CONFLICT. Source
assessments are not used to prefer one source: an
assessment records the analyst's review of a document,
not proof of each statement in it.

Analyst corrections are not handled here: superseded
claims are already excluded by v_current_disclosure_claims,
so any disagreement seen here is between current claims.

Agreement is not corroboration. Several registered sources that
agree are classified by what is known about their independence:
one document registered more than once, documents sharing a
publisher or a recorded dependence, or documents whose
independence is simply unverified. Only agreement between
documents whose separate origin is recorded as provenance is
CONSISTENT_SOURCES (corroborated). Different publishers alone do
not establish independence.
"""

import json
from datetime import date

import pandas as pd

from src.comparison_rules import classify, normalize_claim
from src.source_assessments import (
    PAIR_CONFLICT,
    PAIR_DEPENDENT,
    PAIR_DUPLICATE,
    PAIR_INDEPENDENT,
    evaluate_pair,
)
from src.source_identity import document_keys, publisher_key


REQUIRED_COLUMNS = [
    "claim_id",
    "report_year",
    "period_end",
    "source_id",
    "entity",
    "metric",
    "value",
    "source_location",
]

# Claims are comparable only when all four match exactly.
# Entity names are not fuzzy-matched: similar names may
# be distinct legal entities.
GROUP_COLUMNS = [
    "entity",
    "metric",
    "report_year",
    "period_end",
]

SINGLE_CLAIM = "SINGLE_CLAIM"
REPEATED_SOURCE_CLAIMS = "REPEATED_SOURCE_CLAIMS"
UNRESOLVED_CONFLICT = "UNRESOLVED_CONFLICT"

# Agreement among several registered sources, weakest first.
DUPLICATE_DOCUMENT_CLAIMS = "DUPLICATE_DOCUMENT_CLAIMS"
CONSISTENT_RELATED_SOURCES = "CONSISTENT_RELATED_SOURCES"
INDEPENDENCE_UNVERIFIED = "INDEPENDENCE_UNVERIFIED"
CONSISTENT_SOURCES = "CONSISTENT_SOURCES"
# Recorded assessments disagree and no qualifying pair remains (v3).
CONFLICTING_INDEPENDENCE_ASSESSMENTS = "CONFLICTING_INDEPENDENCE_ASSESSMENTS"

CROSS_SOURCE = "CROSS_SOURCE"
WITHIN_SOURCE = "WITHIN_SOURCE"


def _sort_key(identifier):
    """Order numeric IDs numerically, others lexically."""
    text = str(identifier)
    return (0, int(text), "") if text.isdigit() else (1, 0, text)


def _join_ids(values):
    return ";".join(sorted({str(v) for v in values}, key=_sort_key))


def _prepare(claims):
    missing = set(REQUIRED_COLUMNS) - set(claims.columns)

    if missing:
        raise ValueError(
            f"Missing required columns: {sorted(missing)}"
        )

    if claims.empty:
        raise ValueError("No disclosure claims provided")

    df = claims[REQUIRED_COLUMNS].copy()

    for column in REQUIRED_COLUMNS:
        if df[column].isna().any():
            raise ValueError(f"Missing values in {column}")

        df[column] = df[column].astype(str).str.strip()

        if df[column].eq("").any():
            raise ValueError(f"Empty values in {column}")

    if df["claim_id"].duplicated().any():
        raise ValueError("Duplicate claim IDs provided")

    # Recompute rather than trust any supplied column.
    df["normalized_value"] = [
        normalize_claim(metric, value)
        for metric, value in zip(df["metric"], df["value"])
    ]

    return df


def _conflict_scope(group):
    """Describe where incompatible values occur."""

    scopes = []

    by_source = group.groupby("source_id")["normalized_value"]

    if (by_source.nunique() > 1).any():
        scopes.append(WITHIN_SOURCE)

    value_sets = {
        frozenset(values) for _, values in by_source
    }

    if len(value_sets) > 1:
        scopes.append(CROSS_SOURCE)

    return ";".join(sorted(scopes))


class _Independence:
    """What is known about the independence of registered sources."""

    def __init__(self, sources, related_pairs, independent_pairs, assessments=None):
        sources = list(sources or [])
        # Current recorded assessments by canonical pair (v3), or None.
        self.assessments = None
        if assessments is not None:
            self.assessments = {}
            for a in assessments:
                pair = (int(a["source_a_id"]), int(a["source_b_id"]))
                self.assessments.setdefault(pair, []).append(a)
        self.document = document_keys(sources)
        self.publisher = {
            int(s["source_id"]): publisher_key(s.get("publisher"))
            for s in sources
        }
        self.related = {frozenset(map(int, p)) for p in related_pairs}
        self.independent = {frozenset(map(int, p)) for p in independent_pairs}

    def _document(self, source_id):
        return self.document.get(source_id, f"source:{source_id}")

    def _related(self, a, b):
        pa, pb = self.publisher.get(a), self.publisher.get(b)
        return (pa is not None and pa == pb) or frozenset((a, b)) in self.related

    def agreement(self, source_ids, disclosure=None):
        """Status for agreeing claims from several registered sources."""

        ids = sorted({int(s) for s in source_ids})
        documents = {}
        for source_id in ids:
            documents.setdefault(self._document(source_id), []).append(source_id)

        if self.assessments is not None:
            return self._assessed(ids, documents, disclosure)

        if len(documents) == 1:
            return DUPLICATE_DOCUMENT_CLAIMS

        groups = list(documents.values())
        related = independent = False

        for i, first in enumerate(groups):
            for second in groups[i + 1:]:
                pairs = [(a, b) for a in first for b in second]
                if any(self._related(a, b) for a, b in pairs):
                    related = True
                elif any(frozenset(p) in self.independent for p in pairs):
                    independent = True

        if independent:
            return CONSISTENT_SOURCES
        if related:
            return CONSISTENT_RELATED_SOURCES
        return INDEPENDENCE_UNVERIFIED

    def _assessed(self, ids, documents, disclosure):
        """
        v3: every pair is judged by its current recorded assessments for
        this disclosure, after the automatic blocks. Returns the status
        and every pair outcome, so a dependent or conflicting result
        never hides the assessments behind it.
        """

        document_of = {s: d for d, members in documents.items() for s in members}
        details = {"qualifying_pairs": [], "dependent_pairs": [],
                   "apparent_duplicate_pairs": [], "unverified_pairs": [],
                   "conflicts": []}
        buckets = {PAIR_INDEPENDENT: "qualifying_pairs", PAIR_DEPENDENT: "dependent_pairs",
                   PAIR_DUPLICATE: "apparent_duplicate_pairs", PAIR_CONFLICT: "conflicts"}

        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                automatic = (
                    PAIR_DUPLICATE if document_of[a] == document_of[b]
                    else "SHARED_PUBLISHER" if self._related(a, b) else None
                )
                outcome = evaluate_pair(self.assessments.get((a, b), []), disclosure, automatic)
                entry = {"source_ids": [a, b], **outcome}
                details[buckets.get(outcome["outcome"], "unverified_pairs")].append(entry)

        if len(documents) == 1 and not details["conflicts"]:
            status = DUPLICATE_DOCUMENT_CLAIMS
        elif details["qualifying_pairs"]:
            status = CONSISTENT_SOURCES
        elif details["conflicts"]:
            status = CONFLICTING_INDEPENDENCE_ASSESSMENTS
        elif details["dependent_pairs"] or details["apparent_duplicate_pairs"]:
            status = CONSISTENT_RELATED_SOURCES
        else:
            status = INDEPENDENCE_UNVERIFIED
        return status, details


def reconcile_claims(claims, sources=None, related_source_pairs=(),
                     independent_source_pairs=(), legacy=False, assessments=None):
    """
    Reconcile current claims per comparable disclosure.

    sources: registered source records (source_id, source_url,
    publisher, document_title, publication_date) used to recognise
    duplicate documents and shared publishers. related_source_pairs
    and independent_source_pairs: (source_id, source_id) pairs whose
    dependence or separate origin is recorded as provenance; a shared
    publisher always overrides recorded independence. Without
    provenance, agreeing sources are at most INDEPENDENCE_UNVERIFIED.

    legacy=True reproduces the version-1 rule (distinct source IDs
    agreeing = CONSISTENT_SOURCES) for re-rendering historical
    version-1 snapshots only.

    assessments: current recorded source-relationship assessments
    (version 3). When given, they replace the pair arguments: each
    source pair is judged for the group's own disclosure, and every
    record gains "independence" (JSON) listing qualifying, dependent,
    apparent-duplicate, unverified and conflicting pairs with the
    assessments that decided them. Conflicting assessments block
    corroboration (CONFLICTING_INDEPENDENCE_ASSESSMENTS) unless another
    pair qualifies, and are always listed.

    Returns one row per (entity, metric, report_year,
    period_end) with every contributing claim preserved
    in claim_ids, source_ids, and claim_details (JSON).

    reconciled_value is filled only when all claims agree
    after normalization: the original text when every
    claim reports it identically, otherwise the shared
    normalized value. It is empty for conflicts.
    """

    df = _prepare(claims)
    independence = _Independence(
        sources, related_source_pairs, independent_source_pairs, assessments
    )

    results = []

    for key, group in df.groupby(GROUP_COLUMNS, sort=True):
        assessed = {}
        group = group.sort_values(
            "claim_id", key=lambda s: s.map(_sort_key)
        )

        distinct_values = group["normalized_value"].nunique()
        distinct_sources = group["source_id"].nunique()

        if distinct_values > 1:
            status = UNRESOLVED_CONFLICT
            scope = _conflict_scope(group)
            reconciled = ""

        else:
            scope = ""

            if distinct_sources > 1:
                status = (
                    CONSISTENT_SOURCES if legacy
                    else independence.agreement(group["source_id"], key)
                )
                if isinstance(status, tuple):
                    status, assessed = status
            elif len(group) > 1:
                status = REPEATED_SOURCE_CLAIMS
            else:
                status = SINGLE_CLAIM

            originals = group["value"].unique()
            reconciled = (
                originals[0]
                if len(originals) == 1
                else group["normalized_value"].iloc[0]
            )

        details = [
            {
                "claim_id": row.claim_id,
                "source_id": row.source_id,
                "value": row.value,
                "normalized_value": row.normalized_value,
                "source_location": row.source_location,
            }
            for row in group.itertuples(index=False)
        ]

        record = dict(zip(GROUP_COLUMNS, key))

        record.update({
            "reconciliation_status": status,
            "conflict_scope": scope,
            "reconciled_value": reconciled,
            "claim_count": len(group),
            "source_count": distinct_sources,
            "distinct_value_count": distinct_values,
            # Only agreement of independent sources counts.
            "corroborated": status == CONSISTENT_SOURCES,
            "claim_ids": _join_ids(group["claim_id"]),
            "source_ids": _join_ids(group["source_id"]),
            "source_locations": "; ".join(
                group["source_location"].unique()
            ),
            "claim_details": json.dumps(
                details, ensure_ascii=False
            ),
        })

        if assessments is not None:
            record["independence"] = json.dumps(assessed, ensure_ascii=False,
                                                sort_keys=True, default=str)

        results.append(record)

    return pd.DataFrame(results)


# Year-over-year comparison statuses, in precedence
# order when more than one condition applies.
PERIOD_MISMATCH = "PERIOD_MISMATCH"
MISSING_PERIOD = "MISSING_PERIOD"

STATUS_PRECEDENCE = [
    UNRESOLVED_CONFLICT,
    PERIOD_MISMATCH,
    MISSING_PERIOD,
]

# Period detail codes explaining a PERIOD_MISMATCH.
MULTIPLE_PERIODS_IN_YEAR = "MULTIPLE_PERIODS_IN_YEAR"
INVALID_PERIOD_END = "INVALID_PERIOD_END"
FISCAL_YEAR_END_DIFFERS = "FISCAL_YEAR_END_DIFFERS"
PERIOD_INTERVAL_MISMATCH = "PERIOD_INTERVAL_MISMATCH"


def check_period_compatibility(
    previous_end,
    current_end,
    year_gap,
):
    """
    Decide whether two period-end dates may be compared.

    Returns "" when compatible, otherwise a detail code.

    Compatible means the same fiscal year-end (month and
    day) and period ends exactly year_gap years apart.
    This is a necessary, not sufficient, condition: the
    schema does not record period duration, accounting
    basis, or consolidation scope, so those cannot be
    verified here. A 29 February year-end is treated as
    incompatible with 28 February rather than assumed.
    """

    try:
        previous = date.fromisoformat(str(previous_end))
        current = date.fromisoformat(str(current_end))
    except ValueError:
        return INVALID_PERIOD_END

    if (previous.month, previous.day) != (
        current.month, current.day
    ):
        return FISCAL_YEAR_END_DIFFERS

    if current.year - previous.year != year_gap:
        return PERIOD_INTERVAL_MISMATCH

    return ""


def _side(groups, year):
    """Summarize one reporting year's reconciled groups."""

    single = len(groups) == 1
    resolved = (
        single
        and groups.iloc[0]["reconciliation_status"]
        != UNRESOLVED_CONFLICT
    )

    details = []
    for text in groups["claim_details"]:
        details.extend(json.loads(text))

    return {
        f"value_{year}": (
            groups.iloc[0]["reconciled_value"] if resolved else ""
        ),
        f"source_{year}": _join_ids(
            s for ids in groups["source_ids"] for s in ids.split(";")
        ),
        f"period_end_{year}": ";".join(groups["period_end"]),
        f"location_{year}": "; ".join(groups["source_locations"]),
        f"reconciliation_{year}": ";".join(
            groups["reconciliation_status"]
        ),
        f"claim_ids_{year}": _join_ids(
            c for ids in groups["claim_ids"] for c in ids.split(";")
        ),
        f"evidence_{year}": json.dumps(
            details, ensure_ascii=False
        ),
    }


def build_reconciled_comparison(
    claims,
    previous_year=2024,
    current_year=2025,
    **independence,
):
    """
    Year-over-year comparison over reconciled evidence.

    Accepts every current claim (including corroborating
    and conflicting ones) and returns one row per entity
    and metric. comparison_status is the first applicable
    condition in STATUS_PRECEDENCE, otherwise UNCHANGED or
    CHANGED. comparison_flags lists every condition found,
    so a lower-precedence condition is never hidden.

    independence: passed to reconcile_claims (sources, related and
    independent source pairs, legacy).
    """

    previous_year = str(previous_year)
    current_year = str(current_year)

    if previous_year == current_year:
        raise ValueError("Comparison years must differ")

    reconciled = reconcile_claims(claims, **independence)

    if not reconciled["report_year"].isin(
        [previous_year, current_year]
    ).all():
        raise ValueError("Unexpected reporting year")

    year_gap = int(current_year) - int(previous_year)

    results = []

    for (entity, metric), rows in reconciled.groupby(
        ["entity", "metric"], sort=True
    ):
        previous = rows[rows["report_year"] == previous_year]
        current = rows[rows["report_year"] == current_year]

        flags = []
        period_detail = ""

        if (
            (previous["reconciliation_status"] == UNRESOLVED_CONFLICT).any()
            or (current["reconciliation_status"] == UNRESOLVED_CONFLICT).any()
        ):
            flags.append(UNRESOLVED_CONFLICT)

        # Never pick one of several periods in a year.
        if len(previous) > 1 or len(current) > 1:
            period_detail = MULTIPLE_PERIODS_IN_YEAR

        elif len(previous) == 1 and len(current) == 1:
            period_detail = check_period_compatibility(
                previous.iloc[0]["period_end"],
                current.iloc[0]["period_end"],
                year_gap,
            )

        if period_detail:
            flags.append(PERIOD_MISMATCH)

        if previous.empty or current.empty:
            flags.append(MISSING_PERIOD)

        record = {"entity": entity, "metric": metric}

        for year, groups in (
            (previous_year, previous),
            (current_year, current),
        ):
            if groups.empty:
                record.update({
                    f"value_{year}": "",
                    f"source_{year}": "",
                    f"period_end_{year}": "",
                    f"location_{year}": "",
                    f"reconciliation_{year}": "",
                    f"claim_ids_{year}": "",
                    f"evidence_{year}": "[]",
                })
            else:
                record.update(_side(groups, year))

        change = ""

        if flags:
            status = next(
                s for s in STATUS_PRECEDENCE if s in flags
            )
        else:
            outcome = classify(
                pd.Series({
                    "metric": metric,
                    "value_previous": record[f"value_{previous_year}"],
                    "value_current": record[f"value_{current_year}"],
                    "_merge": "both",
                }),
                previous="value_previous",
                current="value_current",
            )
            status = outcome["comparison_status"]
            change = outcome["change_percentage_points"]

        record.update({
            "comparison_status": status,
            "change_percentage_points": change,
            "comparison_flags": ";".join(flags),
            "period_detail": period_detail,
        })

        results.append(record)

    return pd.DataFrame(results)
