
import json
import pandas as pd
import streamlit as st

import access

from display import utc

from app.gleif_history_ui import render_observed_names

from src.claim_subjects import (
    BASES,
    NAME_TYPES,
    STATUSES,
    current_mappings,
    fetch_case_parties,
    fetch_case_subjects,
    fetch_mappings,
    record_mapping,
    subject_identities,
)


def _load(connect_db, case_id):
    connection = connect_db()
    cursor = connection.cursor(dictionary=True)
    try:
        mappings = fetch_mappings(connection, case_id)
        subjects = [row["subject_text"] for row in fetch_case_subjects(connection, case_id)]
        parties = fetch_case_parties(connection, case_id)
        cursor.execute("SELECT source_id, document_title FROM case_sources "
                       "WHERE case_id = %s ORDER BY source_id", (case_id,))
        sources = cursor.fetchall()
        connection.rollback()
        return mappings, subjects, parties, sources
    finally:
        cursor.close()
        connection.close()


def _save(connect_db, case_id, fields):
    connection = connect_db()
    try:
        access.require_write(connection, case_id)
        connection.rollback()                # record_mapping opens its own transaction
        return record_mapping(connection, case_id, **fields)
    finally:
        connection.close()


def render_claim_subjects(case_id, connect_db):
    """Record and review which legal entity each claim subject refers to."""

    with st.expander("Claim subjects and legal-entity identity"):
        st.caption(
            "Record which legal entity the exact subject text of this case's "
            "claims refers to. Nothing is matched automatically and the claims "
            "are not changed. An identification says nothing about ownership, "
            "control or consolidation. If the text could mean more than one "
            "entity, record AMBIGUOUS; if the entity has no LEI, record NO_LEI."
        )

        try:
            mappings, subjects, parties, sources = _load(connect_db, case_id)
        except Exception as error:
            st.error(f"Claim-subject identities are unavailable: {error}")
            return

        if mappings is None:
            st.info("This database does not record claim-subject identities "
                    "(Milestone 23B.2A is not installed).")
            return
        if not subjects:
            st.info("This investigation has no claims, so there are no subjects to identify.")
            return

        summary = subject_identities(subjects, mappings, parties)
        st.dataframe(pd.DataFrame([{
            "Subject (exact text)": s["subject_text"],
            "Identity": s["status"],
            "LEI": s["lei"] or ", ".join(s["candidate_leis"]) or "—",
            "Notes": " ".join(s["notes"]) or "—",
        } for s in summary["subjects"]]), hide_index=True, width="stretch")
        for orphan in summary["orphaned_mappings"]:
            st.warning(f"A mapping exists for {orphan!r}, but no current claim uses that text.")

        # Read-only: names GLEIF records state for the case parties and for
        # LEIs already named in mappings. Nothing is matched or saved.
        named = set(parties) | {m["lei"] for m in current_mappings(mappings) if m["lei"]}
        for m in current_mappings(mappings):
            if m.get("candidate_leis"):
                candidates = m["candidate_leis"]
                named |= set(json.loads(candidates) if isinstance(candidates, str) else candidates)
        render_observed_names(named, connect_db)

        _render_form(case_id, connect_db, subjects, current_mappings(mappings), sources)

        if mappings:
            st.markdown("**Identity history**")
            current = {int(m["mapping_id"]) for m in current_mappings(mappings)}
            st.dataframe(pd.DataFrame([{
                "Mapping": f"#{m['mapping_id']}",
                "Subject": m["subject_text"],
                "Identity": m["status"],
                "LEI": m["lei"] or "—",
                "Name type": m["name_type"] or "—",
                "Evidence": "; ".join(str(x) for x in (
                    f"S{m['evidence_source_id']}" if m["evidence_source_id"] else None,
                    m["evidence_reference"], m["evidence_location"]) if x) or "None recorded",
                "Rationale": m["rationale"],
                "Limitations": m["limitations"] or "—",
                "Recorded": f"{m['recorded_by']}, {utc(m['recorded_at_utc'])}",
                "Status": "Current" if int(m["mapping_id"]) in current else "Superseded",
            } for m in mappings]), hide_index=True, width="stretch")


def _render_form(case_id, connect_db, subjects, current, sources):
    titles = {int(s["source_id"]): s["document_title"] for s in sources}
    subject = st.selectbox("Subject (exact text from the claims)", subjects,
                           key=f"subject_choice_{case_id}")
    existing = [m for m in current if m["subject_text"] == subject]

    with st.form(f"subject_mapping_form_{case_id}"):
        status = st.selectbox("Identity", STATUSES)
        lei = st.text_input("LEI (for MAPPED)")
        name_type = st.selectbox("The subject text is the entity's", [None, *NAME_TYPES],
                                 format_func=lambda v: "—" if v is None else v)
        candidates = st.text_input("Candidate LEIs (for AMBIGUOUS, comma-separated)")
        basis = st.selectbox("Basis", BASES)
        evidence_source = st.selectbox(
            "Evidence source (registered)", [None, *titles],
            format_func=lambda i: "None" if i is None else f"S{i}: {titles[i]}")
        evidence_reference = st.text_input("Or authoritative registry reference")
        evidence_location = st.text_input("Evidence location")
        rationale = st.text_area("Rationale")
        limitations = st.text_area("Known limitations")
        recorded_by = st.text_input("Recorded by")
        supersede = False
        if existing:
            supersede = st.checkbox(
                f"This supersedes current mapping #{existing[0]['mapping_id']} for this subject")
        access.notice()
        submitted = st.form_submit_button("Record identity", **access.submit_options())

    if not submitted:
        return
    if existing and not supersede:
        st.error("Confirm that the new mapping supersedes the current one.")
        return

    try:
        result = _save(connect_db, case_id, {
            "subject_text": subject, "status": status, "basis": basis,
            "rationale": rationale, "recorded_by": recorded_by,
            "lei": lei.strip() or None, "name_type": name_type,
            "candidate_leis": [c.strip() for c in candidates.split(",") if c.strip()] or None,
            "evidence_source_id": evidence_source,
            "evidence_reference": evidence_reference, "evidence_location": evidence_location,
            "limitations": limitations,
            "supersedes_mapping_id": existing[0]["mapping_id"] if existing else None,
        })
    except Exception as error:
        st.error(f"The identity was not recorded: {error}")
    else:
        st.success(f"Mapping #{result['mapping_id']} recorded." if result["created"]
                   else f"This identity is already recorded as #{result['mapping_id']}.")
