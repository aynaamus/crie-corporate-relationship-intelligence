
"""
Investigation workflow aids (Milestone 23B.3): a read-only overview of the
case's assessment history.
Nothing here writes.
"""

import pandas as pd
import streamlit as st

from display import utc
from src.source_assessments import current_assessments, fetch_assessments

def _load_history(connect_db, case_id):
    connection = connect_db()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT h.history_id, h.source_id, s.document_title, s.source_url, h.event_type, "
            "h.previous_assessment, h.new_assessment, h.review_reason, h.recorded_at_utc, "
            "a.annotation_id IS NOT NULL AS accidental_duplicate "
            "FROM source_assessment_history h JOIN case_sources s ON s.source_id = h.source_id "
            "LEFT JOIN source_history_annotations a ON a.history_id = h.history_id "
            "AND a.annotation_type = 'ACCIDENTAL_DUPLICATE' "
            "WHERE s.case_id = %s ORDER BY h.source_id, h.history_id", (case_id,))
        reviews = cursor.fetchall()
        relationships = fetch_assessments(connection, case_id)
        connection.rollback()
        return reviews, relationships
    finally:
        cursor.close()
        connection.close()


def split_reviews(reviews):
    """
    (investigative entries, annotated accidental duplicates). Only entries
    an investigator annotated as accidental duplicates are set apart; a
    repeated classification with its own reasoning is a genuine revision.
    """

    return ([r for r in reviews if not r["accidental_duplicate"]],
            [r for r in reviews if r["accidental_duplicate"]])


def audit_rows(duplicates):
    """Annotated accidental duplicates, with the identifiers needed to trace them."""

    return [{
        "History ID": r["history_id"],
        "Source": f"S{r['source_id']}: {r['document_title']}",
        "Event": r["event_type"],
        "Previous": r["previous_assessment"] or "—",
        "Assessment": r["new_assessment"],
        "Reasoning": r["review_reason"] or "Not recorded",
        "Recorded": utc(r["recorded_at_utc"]),
        "Annotation": "ACCIDENTAL_DUPLICATE",
    } for r in duplicates]


def review_rows(reviews):
    """Each source's review events; its latest retained event is current."""

    latest = {}
    for review in reviews:
        if not review["accidental_duplicate"]:
            latest[review["source_id"]] = review["history_id"]
    return [{
        "Source": f"S{r['source_id']}: {r['document_title']}",
        "Event": r["event_type"],
        "Previous": r["previous_assessment"] or "—",
        "Assessment": r["new_assessment"],
        "Reasoning": r["review_reason"] or "Not recorded",
        "Recorded": utc(r["recorded_at_utc"]),
        "Evidence": r["source_url"] or "Not recorded",
        "Status": ("Accidental duplicate (annotated, kept)" if r["accidental_duplicate"]
                   else "Current" if latest.get(r["source_id"]) == r["history_id"] else "Earlier"),
    } for r in reviews]


def relationship_rows(assessments):
    """Recorded source-relationship assessments; superseded ones stay listed."""

    current = {a["assessment_id"] for a in current_assessments(assessments)}
    return [{
        "Assessment": f"#{a['assessment_id']}",
        "Sources": f"S{a['source_a_id']} and S{a['source_b_id']}",
        "Relationship": a["relationship"],
        "Basis": a["basis"],
        "Evidence": "; ".join(str(x) for x in (
            f"S{a['evidence_source_id']}" if a["evidence_source_id"] else None,
            a["evidence_reference"], a["evidence_location"]) if x) or "Not recorded",
        "Recorded": utc(a["recorded_at_utc"]),
        "Status": "Current" if a["assessment_id"] in current else "Superseded",
    } for a in assessments]


def render_assessment_history(case_id, connect_db):
    """Every recorded assessment of this case, current and earlier."""

    st.subheader("Assessment history", anchor="crie-assessments")
    st.caption("Evidence reviews and source-relationship assessments recorded for this "
               "investigation, oldest first per source. Earlier entries and superseded "
               "assessments stay listed; entries annotated as accidental duplicates are kept "
               "in the audit record below.")
    try:
        reviews, relationships = _load_history(connect_db, case_id)
    except Exception as error:  # noqa: BLE001 - shown, never hidden
        st.error(f"Assessment history is unavailable: {error}")
        return

    entries, duplicates = split_reviews(reviews)
    st.markdown("**Evidence reviews**")
    if entries:
        st.dataframe(pd.DataFrame(review_rows(entries)), hide_index=True, width="stretch")
    else:
        st.info("No evidence reviews are recorded for this investigation.")
    if duplicates:
        with st.expander(f"Audit record: {len(duplicates)} annotated accidental duplicate "
                         "review(s)"):
            st.caption("Repeated submissions an investigator annotated as accidental "
                       "duplicates. They are kept unchanged in the audit trail and are not "
                       "counted as reviews.")
            st.dataframe(pd.DataFrame(audit_rows(duplicates)), hide_index=True,
                         width="stretch")

    st.markdown("**Source-relationship assessments**")
    if relationships is None:
        st.info("This database does not record source-relationship assessments.")
    elif relationships:
        st.dataframe(pd.DataFrame(relationship_rows(relationships)), hide_index=True,
                     width="stretch")
    else:
        st.info("No source-relationship assessments are recorded for this investigation.")
