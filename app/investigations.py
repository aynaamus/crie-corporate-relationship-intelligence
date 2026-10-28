
"""
My Investigations: the signed-in user's investigations by permanent case ID,
drafts and published, and one investigation's sections shown one at a time.
Every section is an existing view; this page only selects and arranges them.
Only the case's owner sees it here (access.py).

A case is always shown on the GLEIF evidence row it was opened on (its
anchor), whatever later observations report and whether or not the
relationship is still listed as active.
"""

import pandas as pd
import streamlit as st

import access
import case_management
import display
import navigation
from publishing import render_publishing
from case_management import render_case_management
from disclosure_panel import render_disclosure_panel
from report_history import render_report_history
from review_queue import render_relationship_detail
from source_registry import render_source_registry
from src.relationship_context import (
    _EXTRA,
    _JOINS,
    _QUARANTINE,
    ANCHOR_OBSERVATIONS_SQL,
    observation_schema_installed,
    quarantine_schema_installed,
)

SECTIONS = (
    "Case and notes",
    "Relationship and GLEIF history",
    "Sources and assessments",
    "Claims, identities and corrections",
    "Reports",
)

CASES_SQL = """
    SELECT ic.case_id, ic.child_lei, ic.parent_lei, ic.relationship_type,
           ic.snapshot_at_utc, ic.review_status, ic.assessment,
           COALESCE(c.legal_name, ic.child_lei) AS child_name,
           COALESCE(p.legal_name, ic.parent_lei) AS parent_name,
           r.relationship_status AS current_relationship_status{owned_columns}
    FROM investigation_cases ic
    LEFT JOIN legal_entities c ON c.lei = ic.child_lei
    LEFT JOIN legal_entities p ON p.lei = ic.parent_lei
    LEFT JOIN corporate_relationships r
      ON r.child_lei = ic.child_lei AND r.parent_lei = ic.parent_lei
     AND r.relationship_type = ic.relationship_type
    WHERE TRUE{owner_condition}
    ORDER BY ic.case_id
    """

OWNED_COLUMNS = """,
           ic.owner_subject,
           (SELECT COUNT(*) FROM investigation_publications ip
            WHERE ip.case_id = ic.case_id) AS publications"""

# The case's own anchor row, with the provenance columns the review queue
# reads (src/relationship_context.py), whatever the latest observation is.
ANCHOR_SQL = """
    SELECT ic.case_id, ic.review_status, ic.assessment,
           e.child_lei, COALESCE(c.legal_name, e.child_lei) AS child_name,
           c.jurisdiction AS child_jurisdiction,
           e.parent_lei, COALESCE(p.legal_name, e.parent_lei) AS parent_name,
           p.jurisdiction AS parent_jurisdiction,
           e.relationship_type, e.validation_sources, e.validation_documents,
           e.validation_reference, e.last_update_at_utc, e.snapshot_at_utc,
           e.source_file{extra_columns}
    FROM investigation_cases ic
    JOIN relationship_evidence e
      ON e.child_lei = ic.child_lei AND e.parent_lei = ic.parent_lei
     AND e.relationship_type = ic.relationship_type
     AND e.snapshot_at_utc = ic.snapshot_at_utc
    LEFT JOIN legal_entities c ON c.lei = e.child_lei
    LEFT JOIN legal_entities p ON p.lei = e.parent_lei{joins}
    WHERE ic.case_id = %s
    """


def load_cases(connect_db):
    """The user's cases (every case without the ownership schema)."""

    connection = connect_db()
    cursor = connection.cursor(dictionary=True)
    try:
        condition, params = case_management.owner_condition(connection)
        owned = OWNED_COLUMNS if condition else ""
        cursor.execute(CASES_SQL.format(owned_columns=owned, owner_condition=condition), params)
        return pd.DataFrame(cursor.fetchall())
    finally:
        cursor.close()
        connection.rollback()
        connection.close()


def load_anchor(connect_db, case_id):
    """The case's anchor row (read-only), or None if the case does not exist."""

    connection = connect_db()
    cursor = connection.cursor(dictionary=True)
    try:
        installed = observation_schema_installed(connection)
        extra = joins = ""
        if installed:
            quarantine = quarantine_schema_installed(connection)
            extra = _EXTRA.format(quarantine=_QUARANTINE if quarantine else "")
            joins = _JOINS
        cursor.execute(ANCHOR_SQL.format(extra_columns=extra, joins=joins), (case_id,))
        row = cursor.fetchone()
        if row is not None and installed:
            cursor.execute(
                f"SELECT anchor_observations FROM ({ANCHOR_OBSERVATIONS_SQL}) x "
                "WHERE child_lei = %s AND parent_lei = %s AND relationship_type = %s "
                "AND snapshot_at_utc = %s",
                (row["child_lei"], row["parent_lei"], row["relationship_type"],
                 row["snapshot_at_utc"]))
            counted = cursor.fetchone()
            row["anchor_observations"] = counted["anchor_observations"] if counted else None
        return row
    finally:
        cursor.close()
        connection.rollback()
        connection.close()


# What is recorded for a case, counted as stored (labels are plural counts).
INVENTORY = (
    ("GLEIF relationship observations",
     "SELECT COUNT(*) FROM relationship_observations o JOIN investigation_cases ic "
     "ON o.child_lei = ic.child_lei AND o.parent_lei = ic.parent_lei "
     "AND o.relationship_type = ic.relationship_type WHERE ic.case_id = %s"),
    ("registered sources", "SELECT COUNT(*) FROM case_sources WHERE case_id = %s"),
    ("evidence reviews",
     "SELECT COUNT(*) FROM source_assessment_history h JOIN case_sources s USING (source_id) "
     "LEFT JOIN source_history_annotations a ON a.history_id = h.history_id "
     "AND a.annotation_type = 'ACCIDENTAL_DUPLICATE' "
     "WHERE s.case_id = %s AND h.event_type = 'REVIEW' AND a.annotation_id IS NULL"),
    ("current claims",
     "SELECT COUNT(*) FROM v_current_disclosure_claims c JOIN case_sources s "
     "USING (source_id) WHERE s.case_id = %s"),
    ("claim corrections",
     "SELECT COUNT(*) FROM claim_corrections cc JOIN disclosure_claims c "
     "ON c.claim_id = cc.original_claim_id JOIN case_sources s ON s.source_id = c.source_id "
     "WHERE s.case_id = %s"),
    ("source-independence assessments",
     "SELECT COUNT(*) FROM source_relationship_assessments WHERE case_id = %s"),
    ("identity mappings", "SELECT COUNT(*) FROM claim_subject_mappings WHERE case_id = %s"),
    ("registered reports", "SELECT COUNT(*) FROM report_runs WHERE case_id = %s"),
)


def case_inventory(connect_db, case_id):
    """Counts of the case's recorded evidence; a table this database lacks is skipped."""

    found = {}
    try:
        connection = connect_db()
    except Exception:  # noqa: BLE001 - the inventory is optional context
        return found
    cursor = connection.cursor()
    try:
        for label, sql in INVENTORY:
            try:
                cursor.execute(sql, (case_id,))
                found[label] = cursor.fetchone()[0]
            except Exception:  # noqa: BLE001 - e.g. a table not installed here
                continue
    finally:
        cursor.close()
        connection.rollback()
        connection.close()
    return found


def case_table(cases):
    table = {"Case": [f"#{int(i)}" for i in cases["case_id"]]}
    if "publications" in cases:
        table["Visibility"] = ["Published" if n else "Draft" for n in cases["publications"]]
    return pd.DataFrame({
        **table,
        "Reporting entity": cases["child_name"],
        "Relationship": cases["relationship_type"].map(display.relationship_type),
        "Consolidating parent": cases["parent_name"],
        "Status": cases["review_status"].map(display.readable),
        "Analyst assessment (manual)": cases["assessment"].map(display.readable),
        "Anchored on (data valid as of)": cases["snapshot_at_utc"].map(display.utc),
    })


def continuity_notes(case, relationships):
    """How the case's anchor relates to what GLEIF currently reports."""

    notes = []
    if case.get("current_relationship_status") != "ACTIVE":
        notes.append(
            "GLEIF no longer lists this relationship as active "
            f"(recorded status: {display.readable(case.get('current_relationship_status'))}). "
            "The case stays available on the evidence row it was opened on; a change in "
            "status is not evidence of when, or whether, the relationship ended.")
    current = relationships[
        (relationships["child_lei"] == case["child_lei"])
        & (relationships["parent_lei"] == case["parent_lei"])
        & (relationships["relationship_type"] == case["relationship_type"])]
    if not current.empty:
        latest = pd.Timestamp(current.iloc[0]["snapshot_at_utc"])
        if pd.notna(latest) and latest > pd.Timestamp(case["snapshot_at_utc"]):
            notes.append(
                f"A later GLEIF observation (data valid as of {display.utc(latest)}) is "
                "loaded. This case stays anchored on its original evidence row; compare "
                "the observations under Relationship and GLEIF history.")
    return notes


def render_start(relationships, cases, network, company):
    with st.expander("Start an investigation on a relationship",
                     expanded=cases.empty):
        if network:
            rows = relationships[relationships["child_lei"].isin(network)
                                 & relationships["parent_lei"].isin(network)]
            st.caption(f"Relationships in the network of {company}, whatever their "
                       "GLEIF validation class. Change the company on the Investigate page.")
        else:
            rows = relationships
            st.caption("Every loaded relationship. Choose a company on the Investigate page "
                       "to narrow the list to its network.")
        navigation.relationship_picker(rows, navigation.cases_by_relationship(cases),
                                       key="crie_start_relationship")


def render_pending(key, relationships, periods, cases, connect_db):
    """A relationship awaiting its first investigation, opened from another page."""

    existing = navigation.cases_by_relationship(cases).get(key)
    if existing:                                     # never a second case
        st.session_state[navigation.CASE] = existing[-1]
        st.session_state[navigation.PENDING] = None
        st.rerun()

    child, parent, kind = navigation.parse_key(key)
    current = relationships[(relationships["child_lei"] == child)
                            & (relationships["parent_lei"] == parent)
                            & (relationships["relationship_type"] == kind)]
    if current.empty:
        st.warning("This relationship is no longer listed with a current evidence row, "
                   "so a new investigation cannot be anchored on it.")
        return
    row = current.iloc[0]
    st.markdown("#### New investigation")
    render_relationship_detail(row, periods, "Not opened yet")
    render_case_management(row)

    # Once saved, the new case opens like any other.
    opened = case_management.get_relationship_cases(row)
    if opened:
        st.session_state[navigation.CASE] = opened[-1]
        st.session_state[navigation.PENDING] = None
        st.rerun()


def render_case(case_id, cases, relationships, periods, connect_db):
    case = cases[cases["case_id"] == case_id].iloc[0].to_dict()
    anchor = load_anchor(connect_db, case_id)
    if anchor is None:
        st.error(f"The evidence row anchoring Case #{case_id} could not be read.")
        return
    anchor = pd.Series(anchor)
    # Only the user's own cases are listed here; the owner check still guards every write.
    st.session_state[access.CAN_WRITE] = (
        "owner_subject" not in case
        or access.may_write(case["owner_subject"], access.current_user()))

    with st.container(border=True):
        st.caption("Open investigation")
        st.markdown(f"### Case #{case_id} · {case['child_name']} → {case['parent_name']}")
        facts = (
            ("Relationship", display.relationship_type(case["relationship_type"])),
            ("Status", display.readable(case["review_status"])),
            ("Analyst assessment (manual, not computed)", display.readable(case["assessment"])),
            ("Anchored on (GLEIF data valid as of)", display.utc(case["snapshot_at_utc"])),
        )
        for column, (label, value) in zip(st.columns(4), facts):
            column.caption(label)
            column.markdown(f"**{value}**")
        for note in continuity_notes(case, relationships):
            st.info(note)

    sections = list(SECTIONS)
    if st.session_state.get(navigation.SECTION) not in sections:
        st.session_state[navigation.SECTION] = sections[0]
    section = st.radio(
        "Section", sections, horizontal=True,
        index=navigation.index_of(sections, navigation.SECTION),
        key="crie_section", on_change=navigation.keep(navigation.SECTION, "crie_section"))

    if section == SECTIONS[0]:
        render_case_management(anchor)
    elif section == SECTIONS[1]:
        render_relationship_detail(anchor, periods, f"Case #{case_id}")
    elif section == SECTIONS[2]:
        render_source_registry(case_id, connect_db)
    elif section == SECTIONS[3]:
        render_disclosure_panel(case_id, connect_db)
    else:
        render_report_history(case_id, connect_db)
        render_publishing(case_id, connect_db)


def case_picker(cases, key):
    """Choose an investigation by permanent case ID; the choice carries across pages."""

    ids = [int(i) for i in cases["case_id"]]
    names = {int(r["case_id"]): f"Case #{int(r['case_id'])} · {r['child_name']} → "
             f"{r['parent_name']} ({display.relationship_type(r['relationship_type'])})"
             for r in cases.to_dict("records")}
    return st.selectbox(
        "Open an investigation", ids, index=navigation.index_of(ids, navigation.CASE),
        placeholder="Choose a case", key=key, format_func=names.get,
        on_change=navigation.keep(navigation.CASE, key))


def render_investigations(relationships, periods, cases, network, company, connect_db):
    st.title("My Investigations")
    st.caption(
        "Each investigation is a permanent case, opened on one reported relationship and "
        "anchored on the GLEIF evidence row it was opened on. Sources, claims, assessments "
        "and reports are recorded per case; nothing earlier is overwritten. Drafts stay "
        "private: to you when signed in, or to whoever holds a sandbox's link.")

    if st.session_state.get(access.OWNERSHIP) and access.current_user() is None:
        st.info("Sign in, or start a private sandbox, to create investigations and continue "
                "your drafts. Published investigations can be read without either."
                if access.sandbox() else
                "Sign in to create investigations and continue your drafts. Published "
                "investigations can be read without signing in.")
        if access.sandbox() and st.button("Start a private sandbox", type="primary",
                                          key="crie_start_sandbox_mine"):
            access.start_guest()
            st.rerun()
        if access.sign_in_available() and st.button("Sign in", key="crie_sign_in_mine"):
            st.login()
        return

    pending = st.session_state.get(navigation.PENDING)
    if cases.empty:
        st.info("No investigations have been opened yet.")
    else:
        st.dataframe(case_table(cases), hide_index=True, width="stretch")
        case_id = case_picker(cases, "crie_case_picker")
        if case_id is not None:
            st.session_state[navigation.PENDING] = pending = None

    render_start(relationships, cases, network, company)

    pending = st.session_state.get(navigation.PENDING)
    case_id = st.session_state.get(navigation.CASE)
    st.divider()
    if pending:
        render_pending(pending, relationships, periods, cases, connect_db)
    elif case_id is not None and not cases.empty and case_id in set(cases["case_id"]):
        render_case(case_id, cases, relationships, periods, connect_db)
    else:
        st.info("Open an investigation above, or start one on a relationship.")
