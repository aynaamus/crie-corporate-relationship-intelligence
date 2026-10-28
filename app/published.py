
"""
Published Investigations: investigations their owners chose to publish.

A reader sees what was published: the author's title and summary, the
chosen registered report (verified before it is shown) and the public GLEIF
history of the relationship. The case's working records are never shown
here, so later drafts stay private. A flagged GLEIF relationship is not an
investigation and never appears here.
"""

import pandas as pd
import streamlit as st

import access
import display
from gleif_history_ui import render_relationship_history
from publishing import load_published
from report_history import _verify_all, show_verified_report
from src.report_registry import list_registered_runs

OPEN = "crie_open_publication"

FEATURED_QUESTION = (
    "Is Synthetic Harbour Holdings plc reported as consolidated by Synthetic Parent Group AG, "
    "and do the 2024 and 2025 disclosures of the parent's interest agree, once the sources' "
    "independence is taken into account?"
)


def featured(row):
    return row["owner_subject"] == access.DEMO_OWNER


def published_run(connect_db, case_id, run_id):
    connection = connect_db()
    try:
        return next((r for r in list_registered_runs(connection, case_id)
                     if r["run_id"] == run_id), None)
    finally:
        connection.rollback()
        connection.close()


def render_listing(published):
    for row in published.to_dict("records"):
        with st.container(border=True):
            label = "Featured example (synthetic data) · " if featured(row) else ""
            st.caption(f"{label}Published {display.utc(row['published_at_utc'])} by "
                       f"{row['author_name']}")
            st.markdown(f"#### {row['title']}")
            st.write(f"{row['child_name']} → {row['parent_name']} "
                     f"({display.relationship_type(row['relationship_type'])})")
            if st.button("Read this investigation", key=f"read_publication_{row['publication_id']}",
                         type="primary" if featured(row) else "secondary"):
                st.session_state[OPEN] = int(row["publication_id"])
                st.rerun()


def render_publication(row, connect_db):
    if st.button("← All published investigations", key="crie_publications_back"):
        st.session_state[OPEN] = None
        st.rerun()

    st.markdown(f"## {row['title']}")
    st.caption(f"Case #{int(row['case_id'])} · published {display.utc(row['published_at_utc'])} "
               f"by {row['author_name']}"
               + (f" · {int(row['earlier'])} earlier publication(s) remain recorded"
                  if row["earlier"] else ""))
    if featured(row):
        st.info("A prepared example built from synthetic companies, documents and GLEIF files, "
                "to show CRIE's workflow. It describes no real company.")
        st.markdown(f"*Research question:* {FEATURED_QUESTION}")

    with st.container(border=True):
        st.caption("Investigated relationship, as reported to GLEIF")
        st.markdown(f"**{row['child_name']} → {row['parent_name']}** "
                    f"({display.relationship_type(row['relationship_type'])})")
        st.caption(f"Anchored on the GLEIF evidence row with data valid as of "
                   f"{display.utc(row['snapshot_at_utc'])}. Accounting consolidation is not "
                   "evidence of share ownership or control.")

    st.markdown("#### The author's summary")
    with st.container(border=True):
        st.markdown(row["summary"])
    st.caption("The author's interpretation. CRIE verifies the published report and its "
               "evidence, not the author's conclusions.")

    st.markdown("#### Published report")
    run = published_run(connect_db, int(row["case_id"]), row["run_id"])
    if run is None:
        st.error("The published report is not registered in this database.")
        return
    state_key = f"publication_check_{row['publication_id']}"
    if st.button("Verify this report", key=f"verify_publication_{row['publication_id']}"):
        try:
            result = _verify_all(int(row["case_id"]), [run], connect_db, reproducibility=False)
            st.session_state[state_key] = result.get(run["run_id"], {})
        except Exception as error:  # noqa: BLE001 - shown, never hidden
            st.error(f"Verification could not complete: {type(error).__name__}")
    result = st.session_state.get(state_key)
    if result:
        integrity = result.get("integrity", {}).get("status", "Not verified")
        currency = result.get("currency", {}).get("status", "Not verified")
        st.dataframe(pd.DataFrame([{"Report": run["run_id"], "Integrity": integrity,
                                    "Evidence currency": currency,
                                    "Checked": result.get("verified_at_utc", "—")}]),
                     hide_index=True, width="stretch")
        st.caption("INTACT: the report and its manifest match the hashes registered when it "
                   "was created. CURRENT: the investigation's evidence is unchanged since; "
                   "STALE: the author has changed it since, and the published report itself "
                   "is unchanged.")
    show_verified_report(int(row["case_id"]), run)

    render_relationship_history(pd.Series(row), connect_db)


def render_published_page(connect_db):
    st.title("Published Investigations")
    st.caption("Investigations their authors chose to publish, each with a verifiable report. "
               "Drafts are never listed, and neither is a relationship that is merely flagged "
               "by its GLEIF validation class.")

    published = load_published(connect_db)
    if published is None:
        st.info("This database does not record publications.")
        return
    if published.empty:
        st.info("Nothing has been published yet.")
        return

    chosen = st.session_state.get(OPEN)
    rows = published[published["publication_id"] == chosen] if chosen else published.iloc[0:0]
    if rows.empty:
        render_listing(published)
    else:
        render_publication(rows.iloc[0].to_dict(), connect_db)
