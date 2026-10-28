
import pandas as pd
import streamlit as st

import access

from display import utc

from src.source_assessments import (
    BASES,
    DEPENDENT,
    INDEPENDENT_ORIGIN,
    RELATIONSHIPS,
    current_assessments,
    fetch_assessments,
    record_assessment,
    scope_of,
)


PAIR_SCOPE = "All claims of the pair"
DISCLOSURE_SCOPE = "One disclosure"


def _load(connect_db, case_id):
    connection = connect_db()
    cursor = connection.cursor(dictionary=True)
    try:
        assessments = fetch_assessments(connection, case_id)
        cursor.execute(
            "SELECT source_id, document_title FROM case_sources "
            "WHERE case_id = %s ORDER BY source_id", (case_id,))
        sources = cursor.fetchall()
        cursor.execute(
            "SELECT DISTINCT c.source_id, c.entity_name, c.metric, c.report_year, "
            "c.period_end FROM disclosure_claims c JOIN case_sources s "
            "ON s.source_id = c.source_id WHERE s.case_id = %s "
            "ORDER BY c.metric, c.report_year", (case_id,))
        disclosures = cursor.fetchall()
        connection.rollback()
        return assessments, sources, disclosures
    finally:
        cursor.close()
        connection.close()


def _save(connect_db, case_id, fields):
    connection = connect_db()
    try:
        access.require_write(connection, case_id)
        connection.rollback()                # record_assessment opens its own transaction
        return record_assessment(connection, case_id, **fields)
    finally:
        connection.close()


def _conflicts(current):
    """Pairs whose current assessments disagree about independence."""
    by_pair = {}
    for a in current:
        by_pair.setdefault((a["source_a_id"], a["source_b_id"]), []).append(a)
    return [
        pair for pair, items in by_pair.items()
        if any(a["relationship"] in DEPENDENT for a in items)
        and any(a["relationship"] == INDEPENDENT_ORIGIN for a in items)
    ]


def render_source_relationships(case_id, connect_db):
    """Record and review source-relationship assessments (append-only)."""

    with st.expander("Source relationships and independence"):
        st.caption(
            "Record how two of this investigation's sources relate, for "
            "all their claims or for one disclosure. Each entry is kept; "
            "a new view supersedes the current one. Assessments are your "
            "recorded judgement with its basis, not proof. Only a "
            "disclosure-specific INDEPENDENT_ORIGIN with traceable "
            "evidence can support corroboration; if there is no "
            "defensible basis, record UNKNOWN."
        )

        try:
            assessments, sources, disclosures = _load(connect_db, case_id)
        except Exception as error:
            st.error(f"Source relationships are unavailable: {error}")
            return

        if assessments is None:
            st.info(
                "This database does not record source-relationship "
                "assessments (Milestone 23B.1 is not installed)."
            )
            return

        if len(sources) < 2:
            st.info("At least two registered sources are needed.")
            return

        titles = {int(s["source_id"]): s["document_title"] for s in sources}
        current = current_assessments(assessments)
        current_ids = {int(a["assessment_id"]) for a in current}

        for a, b in _conflicts(current):
            st.warning(
                f"Current assessments of S{a} and S{b} disagree about "
                "independence; their agreement is not corroboration."
            )

        _render_form(case_id, connect_db, titles, disclosures, current)

        if assessments:
            st.markdown("**Assessment history**")
            st.dataframe(pd.DataFrame([{
                "Assessment": f"#{a['assessment_id']}",
                "Sources": f"S{a['source_a_id']}, S{a['source_b_id']}",
                "Scope": ("All claims" if a["metric"] is None else
                          f"{a['metric']} {a['report_year']} ({a['period_end']})"),
                "Relationship": a["relationship"],
                "Basis": a["basis"],
                "Evidence": "; ".join(str(x) for x in (
                    f"S{a['evidence_source_id']}" if a["evidence_source_id"] else None,
                    a["evidence_reference"], a["evidence_location"]) if x) or "None recorded",
                "Rationale": a["rationale"],
                "Limitations": a["limitations"] or "—",
                "Recorded": f"{a['recorded_by']}, {utc(a['recorded_at_utc'])}",
                "Status": ("Current" if int(a["assessment_id"]) in current_ids
                           else "Superseded"),
                "Supersedes": (f"#{a['supersedes_assessment_id']}"
                               if a["supersedes_assessment_id"] else "—"),
            } for a in assessments]), hide_index=True, width="stretch")
        else:
            st.write("No source-relationship assessments are recorded yet.")


def _render_form(case_id, connect_db, titles, disclosures, current):
    ids = list(titles)
    label = lambda i: f"S{i}: {titles[i]}"  # noqa: E731

    col1, col2 = st.columns(2)
    first = col1.selectbox("First source", ids, format_func=label,
                           key=f"rel_a_{case_id}")
    second = col2.selectbox("Second source", ids, index=min(1, len(ids) - 1),
                            format_func=label, key=f"rel_b_{case_id}")
    scope_choice = st.radio("Scope", [PAIR_SCOPE, DISCLOSURE_SCOPE], horizontal=True,
                            key=f"rel_scope_{case_id}")

    disclosure = None
    if scope_choice == DISCLOSURE_SCOPE:
        options = {
            (d["entity_name"], d["metric"], int(d["report_year"]), str(d["period_end"])): d
            for d in disclosures if int(d["source_id"]) in (first, second)
        }
        if not options:
            st.info("Neither source has a claim to scope the assessment to.")
            return
        chosen = st.selectbox(
            "Disclosure", list(options),
            format_func=lambda k: f"{k[1]}, {k[2]} (period end {k[3]}) — {k[0]}",
            key=f"rel_disclosure_{case_id}")
        disclosure = dict(zip(("entity_name", "metric", "report_year", "period_end"), chosen))

    pair = (min(first, second), max(first, second))
    scope = None if disclosure is None else (
        disclosure["entity_name"], disclosure["metric"],
        str(disclosure["report_year"]), disclosure["period_end"])
    existing = [a for a in current
                if (a["source_a_id"], a["source_b_id"]) == pair and scope_of(a) == scope]

    with st.form(f"relationship_form_{case_id}"):
        relationship = st.selectbox("Relationship", RELATIONSHIPS)
        basis = st.selectbox("Basis", BASES, help=(
            "DOCUMENTED: stated in the documents or an authoritative register. "
            "INVESTIGATOR_JUDGEMENT: your interpretation."))
        evidence_options = [None, *titles]
        evidence_source = st.selectbox(
            "Evidence source (registered)", evidence_options,
            format_func=lambda i: "None" if i is None else label(i))
        evidence_reference = st.text_input("Or external evidence reference")
        evidence_location = st.text_input("Evidence location (page, note, section)")
        rationale = st.text_area("Rationale")
        limitations = st.text_area("Known limitations")
        recorded_by = st.text_input("Recorded by")
        supersede = False
        if existing:
            supersede = st.checkbox(
                f"This supersedes current assessment #{existing[0]['assessment_id']} "
                "for this pair and scope")
        access.notice()
        submitted = st.form_submit_button("Record assessment", **access.submit_options())

    if not submitted:
        return
    if first == second:
        st.error("Choose two different sources.")
        return
    if existing and not supersede:
        st.error("Confirm that the new assessment supersedes the current one.")
        return

    try:
        result = _save(connect_db, case_id, {
            "source_a_id": first, "source_b_id": second,
            "relationship": relationship, "basis": basis,
            "rationale": rationale, "limitations": limitations,
            "recorded_by": recorded_by,
            "evidence_source_id": evidence_source,
            "evidence_reference": evidence_reference,
            "evidence_location": evidence_location,
            "disclosure": disclosure,
            "supersedes_assessment_id": existing[0]["assessment_id"] if existing else None,
        })
    except Exception as error:
        st.error(f"The assessment was not recorded: {error}")
    else:
        st.success(
            f"Assessment #{result['assessment_id']} recorded."
            if result["created"] else
            f"This assessment is already recorded as #{result['assessment_id']}."
        )
