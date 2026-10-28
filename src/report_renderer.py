
"""
Pure analysis and Markdown rendering of evidence snapshots.

No database access and no file writes. Analytical rules
come from src.claim_reconciliation; nothing here selects
between competing claims.
"""

import json
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from src.claim_corrections import (
    INCOMPLETE,
    NO_CORRECTIONS,
    NO_PROBLEMS_FOUND,
    PROBLEMS_FOUND,
    diagnose_lineages,
)
from src.claim_reconciliation import (
    build_reconciled_comparison,
    reconcile_claims,
)
from src.evidence_snapshot import LINEAGE_NOT_INSTALLED, snapshot_claims_frame
from src.source_assessments import current_assessments
from src.source_identity import TRACKING_PARAMETERS


METRIC_LABELS = {
    "spp_shareholder_interest_pct":
        "Slovenský plynárenský priemysel equity interest",
    "slovak_gas_holding_interest_pct":
        "Slovak Gas Holding B.V. equity interest",
    "management_controller":
        "Reported management controller",
    "included_in_ep_consolidation":
        "Included in EP Infrastructure consolidation",
}


COMPARED = {"UNCHANGED", "CHANGED"}


def _records(frame):
    """DataFrame rows as plain JSON-safe dicts."""
    return json.loads(frame.to_json(orient="records"))


def snapshot_lineage(snapshot):
    """Lineage diagnostics for a version-2 snapshot (pure)."""

    versions = [
        {"claim_id": c["claim_id"], "value_text": c["value"],
         "source_location": c["source_location"],
         "source_id": c["source_id"], "entity": c["entity"],
         "metric": c["metric"], "report_year": c["report_year"],
         "period_end": c["period_end"]}
        for c in snapshot["claims"] + snapshot["historical_claims"]
    ]

    result = diagnose_lineages(
        versions,
        snapshot["corrections"],
        source_ids={s["source_id"] for s in snapshot["sources"]},
        current_ids={c["claim_id"] for c in snapshot["claims"]},
    )
    result["lineage_schema"] = snapshot["lineage_schema"]
    return result


def analyze_snapshot(snapshot, previous_year, current_year):
    """Reconcile and compare the claims in a snapshot."""

    claims = snapshot_claims_frame(snapshot)

    # Version-1 snapshots keep the historical rule (distinct source
    # IDs agreeing = corroborated) so they re-render unchanged;
    # later versions judge agreement by document and publisher.
    version = snapshot["snapshot_schema_version"]
    if version == "1":
        independence = {"legacy": True}
    elif version == "2":
        independence = {"sources": snapshot["sources"]}
    else:
        # Version 3: current recorded assessments; superseded ones stay
        # in the history but never decide anything.
        independence = {
            "sources": snapshot["sources"],
            "assessments": current_assessments(snapshot["source_assessments"]),
        }

    analysis = {
        "reconciliation": _records(reconcile_claims(claims, **independence)),
        "comparison": _records(
            build_reconciled_comparison(
                claims,
                previous_year=previous_year,
                current_year=current_year,
                **independence,
            )
        ),
    }

    # Version-1 analyses stay exactly as they were.
    if snapshot["snapshot_schema_version"] != "1":
        analysis["lineage"] = snapshot_lineage(snapshot)

    return analysis


def clean_url(url):
    """
    Remove tracking parameters for display only. Other
    query parameters and fragments (e.g. #page=18) stay.
    """

    if not url:
        return url

    parts = urlsplit(url)

    # Filter raw segments so the remaining parameters keep
    # their original encoding byte for byte.
    kept = []
    for segment in parts.query.split("&") if parts.query else []:
        key = unquote_plus(segment.split("=", 1)[0]).lower()
        if key.startswith("utm_") or key in TRACKING_PARAMETERS:
            continue
        kept.append(segment)

    return urlunsplit(parts._replace(query="&".join(kept)))


def cell(value):
    """Escape text for a Markdown table cell."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def link_text(value):
    return cell(value).replace("[", "\\[").replace("]", "\\]")


def metric_label(metric):
    return METRIC_LABELS.get(
        metric, metric.replace("_", " ").title()
    )


def format_value(metric, value):
    """Render a reported value; never invent one."""

    if value == "":
        return ""

    text = str(value).strip()

    if metric.endswith("_pct") and not text.endswith("%"):
        return f"{text}%"

    return text


def evidence_refs(evidence):
    """One reference per claim, keeping its own source and location."""

    return ", ".join(
        f"[S{e['source_id']}·C{e['claim_id']}: {e['source_location']}]"
        for e in evidence
    )


def _side_value(row, year):
    value = row[f"value_{year}"]
    reconciliation = row[f"reconciliation_{year}"]

    if not reconciliation:
        return "Not recorded"

    if value == "":
        if "UNRESOLVED_CONFLICT" in reconciliation.split(";"):
            return "Unresolved (see D)"
        return "Not determined (see E)"

    return format_value(row["metric"], value)


def _comparison_table(analysis, previous_year, current_year):
    lines = [
        f"| Metric | Period end {previous_year} | {previous_year} "
        f"| Period end {current_year} | {current_year} "
        "| Change (pp) | Status | Evidence |",
        "|---|---|---|---|---|---:|---|---|",
    ]

    for row in analysis["comparison"]:
        evidence = (
            json.loads(row[f"evidence_{previous_year}"])
            + json.loads(row[f"evidence_{current_year}"])
        )

        change = (
            row["change_percentage_points"]
            if row["metric"].endswith("_pct")
            and row["comparison_status"] in COMPARED
            else "—"
        )

        cells = [
            metric_label(row["metric"]),
            row[f"period_end_{previous_year}"].replace(";", ", ") or "—",
            _side_value(row, previous_year),
            row[f"period_end_{current_year}"].replace(";", ", ") or "—",
            _side_value(row, current_year),
            change,
            row["comparison_status"],
            evidence_refs(evidence) or "—",
        ]

        lines.append("| " + " | ".join(cell(c) for c in cells) + " |")

    return lines


AGREEMENT_WITHOUT_CORROBORATION = (
    "DUPLICATE_DOCUMENT_CLAIMS", "CONSISTENT_RELATED_SOURCES",
    "INDEPENDENCE_UNVERIFIED", "CONFLICTING_INDEPENDENCE_ASSESSMENTS",
)

LEGEND_V1 = [
    "- **SINGLE_CLAIM**: one recorded claim; not corroborated.",
    "- **CONSISTENT_SOURCES**: two or more distinct source "
    "documents report equivalent values (independent "
    "corroboration within the recorded evidence).",
    "- **REPEATED_SOURCE_CLAIMS**: one document records the "
    "same value more than once; not independent corroboration.",
    "- **UNRESOLVED_CONFLICT**: recorded values disagree, across "
    "sources (CROSS_SOURCE) and/or within one source "
    "(WITHIN_SOURCE). No value is selected.",
]

LEGEND = [
    "- **SINGLE_CLAIM**: one recorded claim; not corroborated.",
    "- **REPEATED_SOURCE_CLAIMS**: one registered source records the "
    "same value more than once; not corroboration.",
    "- **DUPLICATE_DOCUMENT_CLAIMS**: the agreeing sources appear to "
    "be registrations of one document (the same URL, or the same "
    "title, publisher and publication date); not corroboration. "
    "A match is a strong indication, not proof of identity.",
    "- **CONSISTENT_RELATED_SOURCES**: sources not recognised as one "
    "document agree but share a publisher or a recorded dependence; "
    "not independent corroboration.",
    "- **INDEPENDENCE_UNVERIFIED**: sources not recognised as one "
    "document agree, but their independence is not established; "
    "they may still be related or republications. Different "
    "publishers alone do not establish independence. Not "
    "corroboration.",
    "- **CONSISTENT_SOURCES**: documents whose separate origin is "
    "recorded as provenance agree (independent corroboration).",
    "- **UNRESOLVED_CONFLICT**: recorded values disagree, across "
    "sources (CROSS_SOURCE) and/or within one source "
    "(WITHIN_SOURCE). No value is selected.",
]

LEGEND_V3 = LEGEND[:-2] + [
    "- **CONFLICTING_INDEPENDENCE_ASSESSMENTS**: recorded assessments, "
    "or an automatic block and an assessment, disagree about whether "
    "the agreeing sources are independent. Not corroboration.",
    "- **CONSISTENT_SOURCES**: at least one pair of agreeing sources has "
    "a current INDEPENDENT_ORIGIN assessment for this specific "
    "disclosure, with recorded evidence, and nothing recorded against "
    "it (independent corroboration of this disclosure by that pair "
    "only; other sources in the group are not thereby independent).",
    LEGEND[-1],
]


def _summary(analysis, legacy=False):
    comparison = analysis["comparison"]
    reconciliation = analysis["reconciliation"]

    counts = {}
    for row in comparison:
        status = row["comparison_status"]
        counts[status] = counts.get(status, 0) + 1

    lines = [
        f"- Metrics compared: {len(comparison)}",
    ]

    for status in sorted(counts):
        lines.append(f"- {status}: {counts[status]} metric(s)")

    corroborated = sum(1 for r in reconciliation if r["corroborated"])
    single = sum(
        1 for r in reconciliation
        if r["reconciliation_status"] == "SINGLE_CLAIM"
    )
    conflicts = sum(
        1 for r in reconciliation
        if r["reconciliation_status"] == "UNRESOLVED_CONFLICT"
    )

    lines.append("")

    if corroborated == 0:
        lines.append(
            "No disclosure in this snapshot is independently "
            "corroborated by more than one source document."
        )
    elif legacy:
        lines.append(
            f"{corroborated} disclosure(s) are reported "
            "consistently by more than one source document."
        )
    else:
        lines.append(
            f"{corroborated} disclosure(s) are corroborated by "
            "documents whose separate origin is recorded."
        )

    agreeing = sum(
        1 for r in reconciliation
        if r["reconciliation_status"] in AGREEMENT_WITHOUT_CORROBORATION
    )
    if agreeing and not legacy:
        lines.append(
            f"{agreeing} disclosure(s) are reported consistently by "
            "more than one registered source without established "
            "independence; this is agreement, not corroboration."
        )

    if single:
        lines.append(
            f"{single} of {len(reconciliation)} disclosure(s) rest "
            "on a single recorded claim."
        )

    if conflicts:
        lines.append(
            f"{conflicts} disclosure(s) contain unresolved "
            "conflicting claims; no value was selected for them."
        )

    lines.extend([
        "",
        "These outcomes describe the recorded claims only. An "
        "UNCHANGED status means the recorded values are equal "
        "across compatible periods; it does not establish that the "
        "wider corporate structure was unchanged.",
    ])

    return lines


def _reconciliation_section(analysis, legacy=False, snapshot=None):
    rows = analysis["reconciliation"]

    lines = [
        "| Metric | Year | Period end | Status | Sources | Claims |",
        "|---|---|---|---|---|---|",
    ]

    for row in rows:
        status = row["reconciliation_status"]
        if row["conflict_scope"]:
            status += f" ({row['conflict_scope'].replace(';', ', ')})"

        details = json.loads(row["claim_details"])

        lines.append("| " + " | ".join(cell(c) for c in [
            metric_label(row["metric"]),
            row["report_year"],
            row["period_end"],
            status,
            ", ".join(sorted(
                {f"S{d['source_id']}" for d in details},
                key=lambda s: int(s[1:]) if s[1:].isdigit() else 0,
            )),
            ", ".join(f"C{d['claim_id']}" for d in details),
        ]) + " |")

    v3 = snapshot is not None and snapshot["snapshot_schema_version"] in ("3", "4")
    lines.extend(["", *(LEGEND_V1 if legacy else LEGEND_V3 if v3 else LEGEND)])

    if v3:
        lines.extend(["", "#### Source independence", "", *_independence_lines(snapshot, rows)])

    conflicts = [
        r for r in rows
        if r["reconciliation_status"] == "UNRESOLVED_CONFLICT"
    ]

    if conflicts:
        lines.extend(["", "#### Unresolved conflicts", ""])

        for row in conflicts:
            lines.append(
                f"**{cell(metric_label(row['metric']))}** — "
                f"{cell(row['entity'])}, {row['report_year']} "
                f"(period end {row['period_end']}), "
                f"scope: {row['conflict_scope'].replace(';', ', ')}"
            )
            lines.append("")

            for d in json.loads(row["claim_details"]):
                lines.append(
                    f"- C{d['claim_id']} from S{d['source_id']}: "
                    f"{cell(format_value(row['metric'], d['value']))} "
                    f"({cell(d['source_location'])})"
                )

            lines.append("")

        lines.append(
            "Conflicting claims are review flags, not findings of "
            "misrepresentation."
        )
    else:
        lines.extend([
            "",
            "No unresolved conflicts were found among the current claims.",
        ])

    return lines


def _independence_lines(snapshot, rows):
    """Recorded source-relationship assessments and what they decided."""

    if snapshot["source_assessments_schema"] != "INSTALLED":
        return ["This database does not record source-relationship "
                "assessments, so no agreement between sources can be "
                "corroborated."]

    assessments = snapshot["source_assessments"]
    if not assessments:
        return ["No source-relationship assessments are recorded for this "
                "investigation, so no agreement between sources is "
                "corroborated."]

    current = {a["assessment_id"] for a in current_assessments(assessments)}
    titles = {s["source_id"]: s["document_title"] for s in snapshot["sources"]}
    lines = [
        "| Assessment | Sources | Scope | Relationship | Basis | Rationale "
        "| Evidence | Recorded | Limitations | Status |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]

    for a in assessments:
        scope = ("all claims of the pair" if a["metric"] is None else
                 f"{metric_label(a['metric'])}, {a['report_year']} "
                 f"(period end {a['period_end']})")
        evidence = "; ".join(x for x in (
            f"S{a['evidence_source_id']} ({titles.get(a['evidence_source_id'], '')})"
            if a["evidence_source_id"] else "",
            a["evidence_reference"] or "",
            a["evidence_location"] or "",
        ) if x) or "none recorded"
        lines.append("| " + " | ".join(cell(x) for x in [
            f"#{a['assessment_id']}",
            f"S{a['source_a_id']}, S{a['source_b_id']}",
            scope, a["relationship"], a["basis"], a["rationale"], evidence,
            f"{a['recorded_by']}, {a['recorded_at_utc']}",
            a["limitations"] or "—",
            "current" if a["assessment_id"] in current else "superseded",
        ]) + " |")

    decisions = []
    for row in rows:
        details = json.loads(row.get("independence") or "{}")
        for label, key in (("supports corroboration", "qualifying_pairs"),
                           ("conflicting assessments", "conflicts")):
            for pair in details.get(key, []):
                ids = [c["assessment_id"] for c in pair["independence"] + pair["dependence"]]
                decisions.append(
                    f"- {cell(metric_label(row['metric']))}, {row['report_year']}: "
                    f"S{pair['source_ids'][0]} and S{pair['source_ids'][1]} — {label}"
                    + (f" (assessments {', '.join(f'#{i}' for i in ids)})" if ids else "")
                    + (f"; automatic block: {pair['automatic']}" if pair["automatic"] else "")
                )

    if decisions:
        lines.extend(["", *decisions])

    lines.extend([
        "",
        "Assessments are investigators' recorded views with their stated "
        "basis and limitations; they are not proof of independence. "
        "Only disclosure-specific INDEPENDENT_ORIGIN assessments can "
        "support corroboration; superseded assessments are shown for "
        "history only.",
    ])
    return lines


def _period_section(analysis, previous_year, current_year):
    flagged = [
        r for r in analysis["comparison"] if r["comparison_flags"]
    ]

    lines = []

    if flagged:
        lines.extend([
            "| Metric | Status | Flags | Period detail |",
            "|---|---|---|---|",
        ])
        for row in flagged:
            lines.append("| " + " | ".join(cell(c) for c in [
                metric_label(row["metric"]),
                row["comparison_status"],
                row["comparison_flags"].replace(";", ", "),
                row["period_detail"] or "—",
            ]) + " |")
    else:
        lines.append(
            "No comparison flags (conflicts, period mismatches, or "
            "missing periods) were raised."
        )

    year_ends = sorted({
        claim_period[5:]
        for r in analysis["comparison"]
        for year in (previous_year, current_year)
        for claim_period in r[f"period_end_{year}"].split(";")
        if claim_period
    })

    if len(year_ends) > 1:
        lines.extend([
            "",
            "The recorded claims use more than one fiscal year-end "
            f"({', '.join(year_ends)}). Each metric is compared only "
            "with the same year-end convention.",
        ])

    lines.extend([
        "",
        "Period compatibility is checked from period-end dates "
        "only. The registry does not record period length, "
        "accounting basis, or consolidation scope, so matching "
        "dates do not prove identical reporting scope.",
    ])

    return lines


def _evidence_cell(snapshot, correction):
    if not correction["attribution_recorded"]:
        return "Not recorded"

    titles = {s["source_id"]: s["document_title"] for s in snapshot["sources"]}
    source_id = correction["evidence_source_id"]
    title = titles.get(source_id, "outside this investigation")
    return f"S{source_id} ({title}): {correction['evidence_location']}"


def _corrections_section_v2(snapshot, lineage):
    corrections = snapshot["corrections"]
    lines = []

    if not corrections:
        lines.append(
            "No corrections are recorded for claims in this "
            "snapshot's reporting years."
        )
    else:
        values = {
            c["claim_id"]: c
            for c in snapshot["claims"] + snapshot["historical_claims"]
        }

        def version(claim_id):
            claim = values.get(claim_id)
            if claim is None:
                return f"C{claim_id} (not available)"
            return (
                f"C{claim_id}: {format_value(claim['metric'], claim['value'])} "
                f"({claim['source_location']})"
            )

        lines += [
            "| Correction | Event | Superseded claim | Replacement claim "
            "| Restored version | Reason | Supporting evidence | Corrected (UTC) |",
            "|---|---|---|---|---|---|---|---|",
        ]

        for c in corrections:
            restoration = c["correction_kind"] == "RESTORATION"
            lines.append("| " + " | ".join(cell(x) for x in [
                f"#{c['correction_id']}",
                "Restoration" if restoration else "Correction",
                version(c["original_claim_id"]),
                version(c["replacement_claim_id"]),
                (f"C{c['restored_claim_id']}" if c["restored_claim_id"]
                 else "not recorded" if restoration else "—"),
                c["correction_reason"] or "—",
                _evidence_cell(snapshot, c),
                c["corrected_at_utc"] or "not recorded",
            ]) + " |")

        lines += [
            "",
            "Superseded claims are analyst corrections of the same "
            "source and are excluded from the analysis above. They are "
            "not competing source claims. A restoration records a new "
            "claim carrying an earlier version's value; the earlier "
            "claim remains in the history. Supporting evidence is the "
            "analyst's attribution, not independent verification.",
        ]

    lines += ["", "#### Lineage diagnostics", "", *_lineage_lines(lineage)]
    return lines


def _lineage_lines(lineage):
    status = lineage["status"]
    scope = (
        f"{lineage['lineages']} lineage(s) covering "
        f"{lineage['corrections']} correction(s)"
    )

    lines = [{
        NO_CORRECTIONS: "**Status: no corrections.** There is no "
                        "correction lineage to evaluate.",
        NO_PROBLEMS_FOUND: f"**Status: no problems found** in {scope}.",
        PROBLEMS_FOUND: f"**Status: problems found** in {scope}:",
        INCOMPLETE: f"**Status: INCOMPLETE.** {scope} could not be fully "
                    "evaluated because referenced claims are unavailable:",
    }[status]]

    if lineage["problems"]:
        lines += ["", *[f"- {cell(p)}" for p in lineage["problems"]]]

    if lineage["lineage_schema"] == LINEAGE_NOT_INSTALLED:
        lines += [
            "",
            "This database does not record correction-lineage metadata: "
            "restorations and supporting evidence are not recorded, and "
            "every correction is shown as recorded by the earlier schema.",
        ]
    elif lineage["attribution_not_recorded"]:
        lines += [
            "",
            f"Supporting evidence was not recorded for "
            f"{lineage['attribution_not_recorded']} of "
            f"{lineage['corrections']} correction(s).",
        ]

    return lines


def _corrections_section(snapshot):
    corrections = snapshot["corrections"]

    if not corrections:
        return [
            "No corrections are recorded for claims in this "
            "snapshot's reporting years."
        ]

    lines = [
        "| Correction | Superseded claim | Replacement claim "
        "| Reason | Corrected (UTC) |",
        "|---|---|---|---|---|",
    ]

    for c in corrections:
        lines.append("| " + " | ".join(cell(x) for x in [
            f"#{c['correction_id']}",
            f"C{c['original_claim_id']}: "
            f"{format_value(c['metric'], c['original_value'])} "
            f"({c['original_location']})",
            f"C{c['replacement_claim_id']}: "
            f"{format_value(c['metric'], c['replacement_value'])} "
            f"({c['replacement_location']})",
            c["correction_reason"] or "—",
            c["corrected_at_utc"] or "not recorded",
        ]) + " |")

    lines.extend([
        "",
        "Superseded claims are analyst corrections of the same "
        "source and are excluded from the analysis above. They are "
        "not competing source claims.",
    ])

    return lines


def _registry_section(snapshot):
    claims_by_source = {}
    for claim in snapshot["claims"]:
        claims_by_source.setdefault(claim["source_id"], []).append(
            f"C{claim['claim_id']}"
        )

    lines = []

    for source in snapshot["sources"]:
        source_id = source["source_id"]
        title = link_text(source["document_title"])
        url = clean_url(source["source_url"])

        heading = (
            f"- **[S{source_id}]** [{title}](<{url}>)"
            if url else f"- **[S{source_id}]** {title}"
        )
        lines.append(heading)

        lines.append(
            f"  - Publisher: {cell(source['publisher'] or 'not recorded')}; "
            "publication date: "
            f"{source['publication_date'] or 'not recorded'}"
        )

        history_id = source["latest_assessment_history_id"]
        latest = next(
            (h for h in source["assessment_history"]
             if h["history_id"] == history_id),
            None,
        )
        recorded = (
            f" (history #{history_id}, {latest['recorded_at_utc']})"
            if latest else " (no assessment history recorded)"
        )
        lines.append(
            f"  - Source-level assessment: "
            f"{source['evidence_assessment']}{recorded}"
        )

        claim_refs = claims_by_source.get(source_id)
        lines.append(
            "  - Claims in this report: "
            + (", ".join(claim_refs) if claim_refs else "none")
        )

    return lines


LIMITATIONS = [
    "- Values are manually structured from registered documents; "
    "the system does not extract or verify document contents.",
    "- A reported fact is what a document states. Independent "
    "corroboration means distinct documents state equivalent values; "
    "it is not proof of accuracy.",
    "- Source-level assessments record an analyst's review of a "
    "document. They do not verify each extracted claim, and they are "
    "never used to choose between conflicting claims.",
    "- Accounting consolidation is not equivalent to legal or "
    "beneficial ownership.",
    "- Text values are compared after case and whitespace "
    "normalization only; differently written names are not assumed "
    "to identify the same entity.",
    "- This report reflects the evidence snapshot identified above. "
    "Later claim, correction, or source changes are not reflected; "
    "compare the snapshot hash with a new run to detect differences.",
]


def _identity_lines(snapshot):
    """Case-specific identity mappings, every state as recorded."""

    identity = snapshot["case_identity"]
    lines = ["#### Case-specific claim-subject identities", "", identity["note"], ""]
    if identity["schema"] != "INSTALLED":
        return lines + ["This database does not record claim-subject identities."]
    if not identity["mappings"]:
        return lines + ["No identity mappings are recorded for this investigation; every "
                        "claim subject is unmapped."]
    superseded = {m["supersedes_mapping_id"] for m in identity["mappings"]}
    lines += ["| Mapping | Subject (exact text) | Identity | LEI or candidates | Basis "
              "| Rationale | Limitations | Recorded | Status |",
              "|---|---|---|---|---|---|---|---|---|"]
    for m in identity["mappings"]:
        leis = m["lei"] or ", ".join(m["candidate_leis"] or ()) or "—"
        lines.append("| " + " | ".join(cell(x) for x in (
            f"#{m['mapping_id']}", m["subject_text"], m["status"], leis, m["basis"],
            m["rationale"], m["limitations"] or "—", f"{m['recorded_by']}, {m['recorded_at_utc']}",
            "Superseded" if m["mapping_id"] in superseded else "Current")) + " |")
    return lines


def _gleif_entry_value(value):
    if value is None:
        return "Not stated"
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "text" in value:
        return value["text"]
    if isinstance(value, list) and value and isinstance(value[0], dict) and "start" in value[0]:
        return "; ".join(f"{p['type']}: {p['start'] or 'start not stated'} to "
                         f"{p['end'] or 'end not stated'}" for p in value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _gleif_context_lines(snapshot):
    """Global GLEIF history, apart from the case's evidence; never corroboration."""

    context = snapshot["gleif_context"]
    lines = ["#### Global GLEIF context (not case evidence)", "", context["note"], ""]
    if context["observation_schema"] != "INSTALLED":
        lines += ["This database records no GLEIF observation history; only legacy "
                  "relationship rows can appear below.", ""]
    history = context["relationship"]
    if history is None:
        lines.append("This investigation records no examined relationship.")
    else:
        subject = history["subject"]
        lines += [f"Relationship: {subject['child_lei']} {subject['relationship_type']} "
                  f"{subject['parent_lei']}.", ""]
        if history["interpretation"]:
            lines += [history["interpretation"], ""]
        if history["entries"]:
            lines += ["| Data valid as of | Source | Origin | Relationship status | Validation "
                      "| Reported periods |", "|---|---|---|---|---|---|"]
            for e in history["entries"]:
                lines.append("| " + " | ".join(cell(x) for x in (
                    e["data_valid_as_of_utc"], e["source"], e["origin"],
                    _gleif_entry_value(e["values"]["relationship_status"]),
                    _gleif_entry_value(e["values"]["validation_sources"]),
                    _gleif_entry_value(e["values"]["periods"]))) + " |")
            lines.append("")
        lines += _gleif_assessment_lines(history)
        for absence in history["absences"]:
            lines.append(f"- Not in {absence['source']} (data valid as of "
                         f"{absence['data_valid_as_of_utc']}): {absence['meaning']}. "
                         f"{absence['note']}")
    for entity in context["entities"]:
        lines += ["", f"Entity {entity['subject']['lei']}: "
                  f"{len(entity['entries'])} published observation(s)."]
        for e in entity["entries"]:
            lines.append(f"- {e['data_valid_as_of_utc']}, {e['source']}: legal name "
                         f"{cell(_gleif_entry_value(e['values']['legal_name']))}")
        lines += _gleif_assessment_lines(entity)
        for event in entity["stated_events"]:
            lines.append(f"- {event['source']} states {event['type']} ({event['event_status']}), "
                         f"effective {event['effective_as_stated'] or 'not stated'}. {event['note']}")
    return lines


def _gleif_assessment_lines(history):
    lines = []
    differing = history["assessment"]["differing_fields"]
    if differing:
        lines.append(f"Published records differ in: {', '.join(differing)}.")
    lines.append(history["assessment"]["note"])
    audit = history["audit"]
    if audit["observations_only_in_incomplete_batches"] or audit["quarantined_records"]:
        lines.append(f"Not shown as evidence: {audit['observations_only_in_incomplete_batches']} "
                     f"observation(s) only in incomplete imports, {audit['quarantined_records']} "
                     "quarantined record(s).")
    return lines


def render_report(snapshot, analysis, metadata):
    """
    Render a Markdown report from a snapshot and its analysis.

    metadata keys: run_id, generated_at_utc, git_commit,
    working_tree_dirty, evidence_snapshot_sha256,
    previous_year, current_year.
    """

    previous_year = metadata["previous_year"]
    current_year = metadata["current_year"]
    legacy = snapshot["snapshot_schema_version"] == "1"

    entities = sorted({c["entity"] for c in snapshot["claims"]})

    dirty = metadata["working_tree_dirty"]
    tree = (
        "unknown" if dirty is None
        else "dirty (uncommitted changes)" if dirty
        else "clean"
    )

    report = [
        "# Corporate Relationship Intelligence — Investigation Report",
        "",
        f"## {cell(', '.join(entities))}: "
        f"{previous_year}–{current_year} Disclosure Comparison",
        "",
        "### A. Report Metadata",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Report run ID | `{metadata['run_id']}` |",
        f"| Generated (UTC) | {metadata['generated_at_utc']} |",
        f"| Investigation case | {snapshot['case_id']} |",
        f"| Compared years | {previous_year}, {current_year} |",
        f"| Code version | `{metadata['git_commit'] or 'unknown'}` |",
        f"| Working tree | {tree} |",
        "| Evidence snapshot SHA-256 | "
        f"`{metadata['evidence_snapshot_sha256']}` |",
        f"| Evidence captured (UTC) | {snapshot['captured_at_utc']} |",
        "",
        "### B. Executive Summary",
        "",
        *_summary(analysis, legacy),
        "",
        "### C. Comparative Disclosures",
        "",
        *_comparison_table(analysis, previous_year, current_year),
        "",
        "Evidence references read `[S<source>·C<claim>: location]`.",
        "",
        "### D. Reconciliation Findings",
        "",
        *_reconciliation_section(analysis, legacy, snapshot),
        "",
        "### E. Reporting-Period Limitations",
        "",
        *_period_section(analysis, previous_year, current_year),
        "",
        "### F. Corrections Applied",
        "",
        *(
            _corrections_section(snapshot)
            if snapshot["snapshot_schema_version"] == "1"
            else _corrections_section_v2(snapshot, analysis["lineage"])
        ),
        "",
        "### G. Evidence Registry",
        "",
        *_registry_section(snapshot),
        "",
        "### H. Methodological Limitations",
        "",
        *LIMITATIONS,
    ]

    # Version 4 only: earlier versions render exactly as before.
    if snapshot["snapshot_schema_version"] == "4":
        report.extend(["", "### I. Case Identity and Global GLEIF Context", "",
                       *_identity_lines(snapshot), "", *_gleif_context_lines(snapshot)])

    return "\n".join(report) + "\n"
