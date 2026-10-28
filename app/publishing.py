
"""
Report versions and publication of an investigation (owners only).

A report version is the existing versioned report (src/generate_versioned_report.py)
of the case's evidence, read in one consistent transaction, written under
the configured runs root and registered (src/report_registry.py). Earlier
versions are never changed.

Publishing appends a row to investigation_publications naming one
registered run of the case, after that run has been verified INTACT. The
database refuses a publication by anyone but the case's owner, or of a run
of another case. A later publication supersedes an earlier one; both stay
recorded.
"""

import pandas as pd
import streamlit as st

import access
import display
from report_history import _verify_all, runs_root
from src.evidence_snapshot import load_evidence_snapshot
from src.generate_versioned_report import generate_versioned_report
from src.report_registry import list_registered_runs, register_run

CREATED = "crie_report_created"           # carries the confirmation across a rerun

PUBLICATIONS_SQL = """
    SELECT p.publication_id, p.case_id, p.run_id, p.title, p.summary, p.author_name,
           p.published_at_utc
    FROM investigation_publications p
    WHERE p.case_id = %s
    ORDER BY p.publication_id DESC
    """


def publication_installed(connection):
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = "
                       "DATABASE() AND TABLE_NAME = 'investigation_publications'")
        return cursor.fetchone()[0] == 1
    finally:
        cursor.close()


def case_publications(connect_db, case_id):
    connection = connect_db()
    cursor = connection.cursor(dictionary=True)
    try:
        if not publication_installed(connection):
            return None
        cursor.execute(PUBLICATIONS_SQL, (case_id,))
        return cursor.fetchall()
    finally:
        cursor.close()
        connection.rollback()
        connection.close()


def claim_years(connect_db, case_id):
    connection = connect_db()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT DISTINCT c.report_year FROM v_current_disclosure_claims c "
                       "JOIN case_sources s USING (source_id) WHERE s.case_id = %s "
                       "ORDER BY c.report_year", (case_id,))
        return [int(row[0]) for row in cursor.fetchall()]
    finally:
        cursor.close()
        connection.rollback()
        connection.close()


def create_report_version(connect_db, case_id, previous_year, current_year):
    """Generate and register a new version 4 report of the case; returns its run ID."""

    connection = connect_db()
    try:
        access.require_write(connection, case_id)
        connection.rollback()
        connection.start_transaction(consistent_snapshot=True,
                                     isolation_level="REPEATABLE READ", readonly=True)
        snapshot = load_evidence_snapshot(case_id=case_id, years=(previous_year, current_year),
                                          connection=connection, version="4")
        connection.rollback()
    finally:
        connection.close()

    run = generate_versioned_report(case_id=case_id, previous_year=previous_year,
                                    current_year=current_year, output_root=runs_root(),
                                    snapshot=snapshot)
    connection = connect_db()
    try:
        access.require_write(connection, case_id)
        connection.rollback()
        register_run(connection, case_id, run["run_id"], runs_root())
    finally:
        connection.close()
    return run["run_id"]


def publish(connect_db, case_id, run, title, summary, author_name):
    """Verify the run, then append the publication; returns the publication ID."""

    user = access.current_user()
    if user is None or access.is_guest(user):
        raise access.AccessDenied("Publishing is for signed-in authors; sandbox work stays "
                                  "private.")
    results = _verify_all(case_id, [run], connect_db, reproducibility=False)
    integrity = results.get(run["run_id"], {}).get("integrity", {}).get("status")
    if integrity != "INTACT":
        raise ValueError(f"The report did not verify (integrity: {integrity or 'not checked'}); "
                         "it cannot be published.")

    connection = connect_db()
    cursor = connection.cursor()
    try:
        access.require_write(connection, case_id, user)
        cursor.execute(
            "INSERT INTO investigation_publications (case_id, run_id, title, summary, "
            "author_name, published_by) VALUES (%s, %s, %s, %s, %s, %s)",
            (case_id, run["run_id"], title.strip(), summary.strip(), author_name.strip(),
             user.subject))
        connection.commit()
        return cursor.lastrowid
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def render_publishing(case_id, connect_db):
    """The owner's report versions and publication controls for one case."""

    st.divider()
    st.subheader("Report versions and publication")
    publications = case_publications(connect_db, case_id)
    connection = connect_db()
    try:
        runs = list_registered_runs(connection, case_id)
    except Exception:  # noqa: BLE001 - registry not installed
        runs = []
    finally:
        connection.rollback()
        connection.close()

    if publications is None:
        st.info("Publishing is not available in this database: it records no investigation "
                "owners. Reports can still be created and verified.")
    elif publications:
        latest = publications[0]
        st.success(f"Published as \"{latest['title']}\" on {display.utc(latest['published_at_utc'])}, "
                   f"with report {latest['run_id']}.")
        if len(publications) > 1:
            st.caption(f"{len(publications) - 1} earlier publication(s) of this investigation "
                       "remain recorded.")
    elif access.is_guest(access.current_user()):
        st.info("Private to your sandbox: only someone with this page's link can see it.")
    else:
        st.info("Draft: only you can see this investigation until you publish it.")

    created = st.session_state.pop(CREATED, None)
    if created:
        st.success(f"Report {created} created and registered. It is listed in the report "
                   "history above, where its integrity can be checked.")

    with st.expander("Create a new report version"):
        st.caption("A version 4 report of the evidence as it is now: claims, sources, "
                   "assessments, identity mappings and GLEIF context. Earlier versions are "
                   "kept unchanged.")
        years = claim_years(connect_db, case_id)
        if not years:
            st.info("Record claims before creating a report.")
        else:
            col1, col2 = st.columns(2)
            # With claims for one year only, compare with the year before it.
            previous = col1.number_input("Previous year", value=years[-2] if len(years) > 1
                                         else years[-1] - 1, step=1, key=f"report_prev_{case_id}")
            current = col2.number_input("Current year", value=years[-1], step=1,
                                        key=f"report_curr_{case_id}")
            if st.button("Create report version", key=f"create_report_{case_id}",
                         **access.submit_options()):
                if previous >= current:
                    st.error("The previous year must come before the current year.")
                else:
                    try:
                        run_id = create_report_version(connect_db, case_id, int(previous),
                                                       int(current))
                    except Exception as error:  # noqa: BLE001 - shown, never hidden
                        st.error(f"The report was not created: {error}")
                    else:
                        # Rerun so the report history above lists the new version.
                        st.session_state[CREATED] = run_id
                        st.rerun()

    if publications is None or not runs:
        return

    if access.is_guest(access.current_user()):
        st.caption("Publishing is for signed-in authors. Work in a sandbox stays private: "
                   "your reports can be verified and read here, by you.")
        return

    with st.expander("Publish this investigation", expanded=False):
        st.caption("Publishing makes the chosen report, your title and your summary readable "
                   "by every visitor. Your working records stay private. The report is "
                   "verified before it is published, and nothing is deleted when you publish "
                   "again later.")
        user = access.current_user()
        with st.form(f"publish_form_{case_id}"):
            run_id = st.selectbox("Report to publish", [r["run_id"] for r in runs],
                                  format_func=lambda r: f"{r} (generated "
                                  f"{display.utc(next(x for x in runs if x['run_id'] == r)['generated_at_utc'])})")
            title = st.text_input("Title")
            summary = st.text_area("Your findings and their limits",
                                   help="Your interpretation, as the author. CRIE verifies the "
                                        "report and its evidence, not your conclusions.")
            author = st.text_input("Author name shown to readers",
                                   value=user.name if user else "")
            confirmed = st.checkbox("I have reviewed this report and want to publish it.")
            access.notice()
            submitted = st.form_submit_button("Publish", type="primary",
                                              **access.submit_options())
        if submitted:
            if not (title.strip() and summary.strip() and author.strip()):
                st.error("A title, a summary and an author name are required.")
            elif not confirmed:
                st.error("Confirm that you want to publish this report.")
            else:
                run = next(r for r in runs if r["run_id"] == run_id)
                try:
                    publication_id = publish(connect_db, case_id, run, title, summary, author)
                except Exception as error:  # noqa: BLE001 - shown, never hidden
                    st.error(f"Not published: {error}")
                else:
                    st.success(f"Published (publication #{publication_id}).")
                    st.rerun()


PUBLISHED_SQL = """
    SELECT p.publication_id, p.case_id, p.run_id, p.title, p.summary, p.author_name,
           p.published_at_utc, ic.child_lei, ic.parent_lei, ic.relationship_type,
           ic.snapshot_at_utc, ic.owner_subject,
           COALESCE(c.legal_name, ic.child_lei) AS child_name,
           COALESCE(pe.legal_name, ic.parent_lei) AS parent_name,
           (SELECT COUNT(*) FROM investigation_publications e
            WHERE e.case_id = p.case_id AND e.publication_id < p.publication_id) AS earlier
    FROM investigation_publications p
    JOIN investigation_cases ic ON ic.case_id = p.case_id
    LEFT JOIN legal_entities c ON c.lei = ic.child_lei
    LEFT JOIN legal_entities pe ON pe.lei = ic.parent_lei
    WHERE p.publication_id = (SELECT MAX(p2.publication_id) FROM investigation_publications p2
                              WHERE p2.case_id = p.case_id)
    ORDER BY ic.owner_subject = %s DESC, p.published_at_utc DESC, p.publication_id DESC
    """


def load_published(connect_db):
    """Each published investigation's current publication; None without the schema."""

    connection = connect_db()
    cursor = connection.cursor(dictionary=True)
    try:
        if not publication_installed(connection):
            return None
        cursor.execute(PUBLISHED_SQL, (access.DEMO_OWNER,))
        return pd.DataFrame(cursor.fetchall())
    finally:
        cursor.close()
        connection.rollback()
        connection.close()
