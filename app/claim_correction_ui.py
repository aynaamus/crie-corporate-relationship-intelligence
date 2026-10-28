
import pandas as pd
import streamlit as st

import access

from display import utc

from src.claim_corrections import (
    INCOMPLETE,
    NO_CORRECTIONS,
    NO_PROBLEMS_FOUND,
    PROBLEMS_FOUND,
    correct_claim,
    fetch_case_diagnostics,
    fetch_lineage,
    lineage_installed,
    restore_claim,
)


CORRECT = "Correct the value or location"
RESTORE = "Restore an earlier version"

HOW_CORRECTIONS_WORK = (
    "A stored claim is never edited. A correction records a new version of the claim, with "
    "your reason and, where you give it, the supporting evidence; the version it replaces "
    "stays recorded as superseded.",
    "Restoring brings back an earlier version's value as another new version. It is itself "
    "a recorded correction: nothing is undone or deleted.",
    "Reconciliation, comparisons and new reports use only the active version. Reports already "
    "generated are not changed; their verification shows whether present evidence differs.",
)


def claim_status_lines(selected, lineage):
    """Plain statements of what is recorded for a claim and what can be done."""

    versions = lineage["versions"]
    lines = [f"Recorded now: claim #{selected['claim_id']} (the active version), value "
             f"\"{selected['value_text']}\" at \"{selected['source_location']}\"."]
    if len(versions) > 1:
        lines.append(f"This disclosure has {len(versions)} recorded versions; the "
                     f"{len(versions) - 1} earlier one(s) are superseded and kept unchanged.")
    else:
        lines.append("This is the only recorded version; it has never been corrected.")
    earlier = [v for v in versions[:-1]
               if (v["value_text"], v["source_location"])
               != (selected["value_text"], selected["source_location"])]
    lines.append("You can record a correction (a new version with a reason)"
                 + (" or restore an earlier version." if earlier else
                    ". Restoring is not available: no earlier version differs from this one."))
    return lines


def _read(connect_db, query, params):
    connection = connect_db()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(query, params)
        return cursor.fetchall()
    finally:
        cursor.close()
        connection.close()


def _load_claims(connect_db, case_id):
    # Load only current claims.
    return _read(
        connect_db,
        """
        SELECT
            c.claim_id,
            c.entity_name,
            c.metric,
            c.report_year,
            c.value_text,
            c.source_location,
            c.source_id,
            s.document_title,
            s.source_url
        FROM v_current_disclosure_claims c
        JOIN case_sources s
            ON s.source_id = c.source_id
        WHERE s.case_id = %s
        ORDER BY c.claim_id
        """,
        (case_id,),
    )


def _load_sources(connect_db, case_id):
    return _read(
        connect_db,
        "SELECT source_id, document_title FROM case_sources "
        "WHERE case_id = %s ORDER BY source_id",
        (case_id,),
    )


def _load_lineage(connect_db, case_id, claim_id):
    connection = connect_db()
    try:
        installed = lineage_installed(connection)
        lineage = fetch_lineage(connection, case_id, claim_id) if installed else None
        connection.rollback()
        return installed, lineage
    finally:
        connection.close()


def _load_diagnostics(connect_db, case_id):
    connection = connect_db()
    try:
        result = fetch_case_diagnostics(connection, case_id)
        connection.rollback()
        return result
    finally:
        connection.close()


def _render_diagnostics(case_id, connect_db):
    """Read-only lineage summary for the whole investigation."""

    with st.expander("Lineage diagnostics"):
        try:
            result = _load_diagnostics(connect_db, case_id)
        except Exception as error:
            st.error(f"Lineage diagnostics are unavailable: {error}")
            return

        if result["lineage_schema"] != "INSTALLED":
            st.info(
                "This database does not record correction-lineage "
                "metadata (Milestone 22.4A is not installed): restorations "
                "and supporting evidence are not recorded. Only the "
                "structure of recorded corrections is checked."
            )

        status = result["status"]
        scope = (
            f"{result['lineages']} lineage(s), "
            f"{result['corrections']} correction(s)"
        )

        if status == NO_CORRECTIONS:
            st.write("No corrections are recorded for this investigation.")
        elif status == NO_PROBLEMS_FOUND:
            st.success(f"No lineage problems found ({scope}).")
        elif status == PROBLEMS_FOUND:
            st.error(f"Lineage problems found ({scope}).")
        elif status == INCOMPLETE:
            st.warning(
                f"Incomplete: referenced claims are unavailable, so the "
                f"lineage could not be fully evaluated ({scope})."
            )

        for problem in result["problems"]:
            st.write(f"- {problem}")

        if result["lineage_schema"] == "INSTALLED" and result["attribution_not_recorded"]:
            st.caption(
                f"Supporting evidence was not recorded for "
                f"{result['attribution_not_recorded']} of "
                f"{result['corrections']} correction(s)."
            )


def _load_history(connect_db, case_id):
    return _read(
        connect_db,
        """
        SELECT
            cc.correction_id,
            old.claim_id AS original_claim_id,
            old.value_text AS original_value,
            newer.claim_id AS replacement_claim_id,
            newer.value_text AS corrected_value,
            cc.correction_reason,
            cc.corrected_at_utc
        FROM claim_corrections cc
        JOIN disclosure_claims old
            ON old.claim_id = cc.original_claim_id
        JOIN disclosure_claims newer
            ON newer.claim_id = cc.replacement_claim_id
        JOIN case_sources s
            ON s.source_id = old.source_id
        WHERE s.case_id = %s
        ORDER BY cc.correction_id DESC
        """,
        (case_id,),
    )


def _render_lineage(lineage):
    versions = lineage["versions"]
    active = lineage["active_claim_id"]

    st.markdown("**Claim versions**")
    st.dataframe(
        pd.DataFrame([
            {
                "Claim": f"#{v['claim_id']}",
                "Value": v["value_text"],
                "Location": v["source_location"],
                "Recorded": utc(v.get("created_at"), missing="Not recorded"),
                "Status": "Active" if v["claim_id"] == active else "Superseded",
            }
            for v in versions
        ]),
        hide_index=True,
        width="stretch",
    )

    if lineage["transitions"]:
        st.markdown("**Correction history**")
        st.dataframe(
            pd.DataFrame([
                {
                    "Event": (
                        f"Restored #{t['restored_claim_id']}"
                        if t["correction_kind"] == "RESTORATION"
                        else "Correction"
                    ),
                    "From": f"#{t['original_claim_id']}",
                    "To": f"#{t['replacement_claim_id']}",
                    "Reason": t["correction_reason"],
                    "Evidence": (
                        f"{t.get('evidence_document_title') or 'Source'}"
                        f" — {t['evidence_location']}"
                        if t.get("evidence_location") else "Not recorded"
                    ),
                    "Recorded": utc(t["corrected_at_utc"]),
                }
                for t in lineage["transitions"]
            ]),
            hide_index=True,
            width="stretch",
        )

    for problem in lineage["problems"]:
        st.warning(f"Lineage problem: {problem}")


def _save(connect_db, action, **arguments):
    write_connection = None

    try:
        write_connection = connect_db()
        access.require_write(write_connection, arguments["case_id"])
        write_connection.rollback()          # the action opens its own transaction
        return action(connection=write_connection, **arguments)
    finally:
        if write_connection is not None:
            write_connection.close()


def render_claim_correction_ui(case_id, connect_db):
    st.divider()
    st.subheader("Claim Corrections", anchor="crie-corrections")

    st.caption(
        "Correct transcription mistakes and source locations "
        "without overwriting original evidence."
    )

    try:
        claims = _load_claims(connect_db, case_id)
    except Exception as error:
        st.error(f"Unable to load claims: {error}")
        return

    if claims:
        claim_map = {
            int(row["claim_id"]): row
            for row in claims
        }

        selected_id = st.selectbox(
            "Select a claim to correct",
            options=list(claim_map),
            format_func=lambda claim_id: (
                f"Claim #{claim_id} — "
                f"{claim_map[claim_id]['metric']} "
                f"({claim_map[claim_id]['report_year']})"
            ),
            key=f"correct_claim_selection_{case_id}",
        )

        selected = claim_map[selected_id]

        st.write(
            f"**Current value:** {selected['value_text']}"
        )

        st.write(
            f"**Source:** {selected['document_title']}"
        )

        st.write(
            f"**Current location:** "
            f"{selected['source_location']}"
        )

        st.markdown(
            f"[Open source document]"
            f"({selected['source_url']})"
        )

        try:
            installed, lineage = _load_lineage(connect_db, case_id, selected_id)
            sources = _load_sources(connect_db, case_id)
        except Exception as error:
            st.error(f"Unable to load the claim's history: {error}")
            return

        if not installed:
            st.info(
                "Claim corrections are paused: the correction-lineage "
                "migration (Milestone 22.4A) is not installed in this "
                "database. Existing history is shown below."
            )
        else:
            for line in claim_status_lines(selected, lineage):
                st.write(line)
            with st.expander("How claim corrections work"):
                for line in HOW_CORRECTIONS_WORK:
                    st.markdown(f"- {line}")
            _render_lineage(lineage)
            _render_form(case_id, connect_db, selected, lineage, sources)

    _render_diagnostics(case_id, connect_db)

    try:
        history = _load_history(connect_db, case_id)
    except Exception as error:
        st.error(
            f"Unable to retrieve correction history: {error}"
        )
        history = []

    with st.expander(
        f"Correction History ({len(history)})"
    ):
        if history:
            st.dataframe(
                pd.DataFrame(history),
                hide_index=True,
                width="stretch",
            )
        else:
            st.info(
                "No corrections have been recorded."
            )


def _render_form(case_id, connect_db, selected, lineage, sources):
    selected_id = int(selected["claim_id"])
    earlier = [
        v for v in lineage["versions"][:-1]
        if (v["value_text"], v["source_location"])
        != (selected["value_text"], selected["source_location"])
    ]
    source_titles = {int(s["source_id"]): s["document_title"] for s in sources}

    actions = [CORRECT] + ([RESTORE] if earlier else [])
    with st.expander("Record a correction or restoration"):
        action = st.radio(
            "Action",
            actions,
            horizontal=True,
            key=f"correction_action_{case_id}_{selected_id}",
        )

        with st.form(
            f"claim_correction_form_{case_id}_{selected_id}"
        ):
            if action == RESTORE:
                versions = {int(v["claim_id"]): v for v in earlier}
                restored_id = st.selectbox(
                    "Earlier version to restore",
                    options=list(versions),
                    format_func=lambda i: (
                        f"Claim #{i}: {versions[i]['value_text']} "
                        f"({versions[i]['source_location']})"
                    ),
                )
                st.caption(
                    "Restoring creates a new claim with the earlier value; "
                    "the earlier claim stays in the history."
                )
            else:
                new_value = st.text_input(
                    "Corrected value",
                    value=str(selected["value_text"]),
                )

                new_location = st.text_input(
                    "Corrected source location",
                    value=str(selected["source_location"]),
                )

            evidence_source = st.selectbox(
                "Supporting evidence source",
                options=list(source_titles) or [int(selected["source_id"])],
                index=(
                    list(source_titles).index(int(selected["source_id"]))
                    if int(selected["source_id"]) in source_titles else 0
                ),
                format_func=lambda i: source_titles.get(i, f"Source #{i}"),
            )

            evidence_location = st.text_input(
                "Where the evidence appears in that source",
                placeholder="Page, note or section",
            )

            reason = st.text_area(
                "Reason for correction",
                placeholder=(
                    "Explain the mistake and identify "
                    "the supporting disclosure."
                ),
            )

            verified = st.checkbox(
                "I have checked the original source and confirm that "
                "this correction and its evidence are recorded accurately."
            )
            st.caption(
                "This is your acknowledgement as the analyst. CRIE records "
                "it with the cited evidence; it does not verify the "
                "correction automatically."
            )

            access.notice()
            submitted = st.form_submit_button(
                "Save audited correction",
                type="primary",
                **access.submit_options(),
            )

    if not submitted:
        return

    if not verified:
        st.error(
            "Verify the source before saving."
        )
        return

    evidence = {
        "evidence_source_id": evidence_source,
        "evidence_location": evidence_location,
    }

    try:
        if action == RESTORE:
            result = _save(
                connect_db, restore_claim,
                case_id=case_id,
                current_claim_id=selected_id,
                restored_claim_id=restored_id,
                reason=reason,
                **evidence,
            )
        else:
            result = _save(
                connect_db, correct_claim,
                case_id=case_id,
                original_claim_id=selected_id,
                new_value=new_value,
                new_location=new_location,
                reason=reason,
                **evidence,
            )

    except Exception as error:
        st.error(
            f"Correction was not saved: {error}"
        )

    else:
        st.success(
            "Correction saved. "
            f"Original claim "
            f"#{result['original_claim_id']} "
            "was superseded by claim "
            f"#{result['replacement_claim_id']}."
        )
        st.rerun()
