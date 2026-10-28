
import pandas as pd
import streamlit as st
from app.case_management import connect_db
from app.gleif_history_ui import render_relationship_history

from app import display as labels
import navigation
from src.gleif_observations import batch_coverage

COVERAGE_TEXT = {
    "COMPLETE_PUBLISHED_DATASET": "a complete import of a full published GLEIF file",
    "COMPLETE_DELTA_FILE": "a complete import of a GLEIF delta file (changes only)",
    "COMPLETE_FILE_SCOPE_UNKNOWN": "a complete import of a file whose scope is not stated",
    "COMPLETE_SAMPLE_OF_FILE": "a sample of a GLEIF file, not the complete dataset",
    "COMPLETE_FILE_SCAN_WITH_QUARANTINED_RECORDS":
        "a complete scan of a GLEIF file in which some records were quarantined (not a fully "
        "trusted dataset)",
}


def value(case, field):
    """A field of the selected row, None when absent or missing."""

    item = case.get(field) if hasattr(case, "get") else None
    return None if item is None or (not isinstance(item, (list, dict)) and pd.isna(item)) else item


def anchor_provenance(case):
    """How the evidence row shown for a relationship came to exist."""

    origin = value(case, "anchor_origin")
    if origin != "GLEIF_BATCH":
        return {"origin": origin, "batch_id": None, "coverage": None, "observations": None}
    coverage = batch_coverage("COMPLETED", value(case, "anchor_selection_mode"),
                              value(case, "anchor_publication_scope"),
                              int(value(case, "anchor_quarantined_records") or 0))
    return {"origin": origin, "batch_id": int(value(case, "anchor_batch_id")),
            "coverage": coverage, "observations": value(case, "anchor_observations")}

def format_date(value):
    if value is None or pd.isna(value):
        return "Not reported"

    return pd.Timestamp(value).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def render_gleif_history(case):
    """The read-only GLEIF source history of the selected relationship."""

    render_relationship_history(case, connect_db)


def render_relationship_summary(case, investigation, action=None):
    """The selected relationship as reported to GLEIF, and its investigation."""

    with st.container(border=True):
        st.caption("Selected relationship, as reported to GLEIF")
        st.markdown(
            f"### {case['child_name']} → "
            f"{case['parent_name']}"
        )

        col1, col2, col3 = st.columns(3)

        with col1:
            st.write("**Relationship type**")
            st.write(labels.relationship_type(case["relationship_type"]))
            st.caption(f"`{case['relationship_type']}`")

        with col2:
            st.write("**GLEIF validation class**")
            st.write(labels.gleif_validation(case["validation_sources"]))
            st.caption("GLEIF's own classification, not a CRIE corroboration finding.")

        with col3:
            st.write("**Validation documents**")
            st.write(labels.validation_documents(case["validation_documents"]))

        col1, col2, col3 = st.columns(3)

        with col1:
            st.write("**Data valid as of (GLEIF ContentDate)**")
            st.write(format_date(case["snapshot_at_utc"]))

        with col2:
            st.write("**Last GLEIF record update**")
            st.write(format_date(case["last_update_at_utc"]))

        with col3:
            st.write("**Investigation**")
            st.write(investigation)

        if action is not None:
            action()


SCOPE_NETWORK = "Selected network"
SCOPE_FLAGGED = "Whole dataset (flagged)"


def render_review_queue(relationships, periods, cases=None, network=None, company=None):
    """
    The review worklist: every relationship in the selected company's network,
    whatever its GLEIF class, or the whole dataset's relationships outside
    GLEIF's FULLY_CORROBORATED class.
    """

    st.subheader("Research leads")

    scopes = [SCOPE_NETWORK, SCOPE_FLAGGED] if network else [SCOPE_FLAGGED]
    # A separate key without a company, so choosing one opens on its network.
    scope = st.radio("Show", scopes, horizontal=True,
                     key="crie_queue_scope" if network else "crie_queue_scope_all")

    if scope == SCOPE_NETWORK:
        st.caption(
            f"Every reported relationship in the network of {company}, whatever its "
            "GLEIF validation class."
        )
        review = relationships[
            relationships["child_lei"].isin(network)
            & relationships["parent_lei"].isin(network)
        ].copy().reset_index(drop=True)
        label, help_text = ("Relationships in the selected network",
                            "Reported relationships among the selected company's network.")
    else:
        st.caption(
            "Relationships in the loaded GLEIF dataset (not only the "
            "selected entity) whose GLEIF validation class is not "
            "FULLY_CORROBORATED. That class is GLEIF's own validation "
            "category, not a CRIE corroboration finding. These flags "
            "do not indicate wrongdoing."
            + ("" if network else " Choose a company in the sidebar to list its whole network.")
        )
        # Select records outside GLEIF's FULLY_CORROBORATED class.
        review = relationships[
            relationships["validation_sources"]
            .fillna("")
            .ne("FULLY_CORROBORATED")
        ].copy().reset_index(drop=True)
        label, help_text = ("Flagged relationships (whole dataset)",
                            "Relationships outside GLEIF's FULLY_CORROBORATED validation "
                            "class, across every loaded company.")

    st.metric(label, len(review), help=help_text)

    if review.empty:
        st.info(
            "Every loaded relationship is in GLEIF's "
            "FULLY_CORROBORATED validation class."
            if scope == SCOPE_FLAGGED else
            "No reported relationships are loaded for this network."
        )
        return

    # Existing investigations first; otherwise in dataset order. Rows are
    # identified by their relationship key, never by position.
    opened = navigation.cases_by_relationship(cases)
    rows = {navigation.relationship_key(r): r for r in review.to_dict("records")}
    choices = sorted(rows, key=lambda k: k not in opened)

    queue = labels.relationship_table(
        pd.DataFrame([rows[k] for k in choices]), documents=False)
    queue.insert(0, "Investigation", [
        ", ".join(f"Case #{i}" for i in opened[k]) if k in opened else "—" for k in choices])
    st.dataframe(
        queue,
        hide_index=True,
        width="stretch"
    )

    st.divider()

    st.subheader("Investigate a relationship", anchor="crie-relationship")

    if st.session_state.get(navigation.RELATIONSHIP) not in rows:
        st.session_state[navigation.RELATIONSHIP] = None
    selected_key = st.selectbox(
        "Select an investigation",
        options=choices,
        index=navigation.index_of(choices, navigation.RELATIONSHIP),
        placeholder="Choose an existing investigation or a relationship",
        key="crie_selected_relationship",
        on_change=navigation.keep(navigation.RELATIONSHIP, "crie_selected_relationship"),
        format_func=lambda k: navigation.describe(rows[k], opened),
    )

    if selected_key is None:
        st.info(
            f"{len([k for k in choices if k in opened])} existing investigation(s) are "
            "listed first. Choose one to continue it, or choose a relationship to examine "
            "its evidence and open a new investigation."
            if any(k in opened for k in choices) else
            "Choose a relationship to examine its evidence and open an investigation."
        )
        return

    case = pd.Series(rows[selected_key])
    investigation = (", ".join(f"Case #{i}" for i in opened[selected_key])
                     if selected_key in opened else "Not opened yet")
    render_relationship_detail(
        case, periods, investigation,
        action=lambda: navigation.investigate_action(case, opened, key="crie_queue_investigate"))


def render_relationship_detail(case, periods, investigation, action=None):
    """Everything recorded about one relationship's evidence row (read-only)."""

    render_relationship_summary(case, investigation, action)

    provenance = anchor_provenance(case)
    if provenance["origin"] == "GLEIF_BATCH":
        st.warning(
            f"This evidence row was created by GLEIF import batch "
            f"#{provenance['batch_id']}, {COVERAGE_TEXT[provenance['coverage']]}. "
            "Its reported periods are those of that batch's observation (not the "
            "legacy period table). A later or different GLEIF file may report this "
            "relationship differently; see the GLEIF source history below. The file's "
            "data is valid as of its ContentDate; that is not the date of any "
            "corporate change."
        )
        if provenance["observations"] and int(provenance["observations"]) > 1:
            st.warning(
                f"The source file reported this relationship in "
                f"{int(provenance['observations'])} different ways. This evidence row "
                "reflects the first; the periods below are from every version."
            )

    reference = case.get("validation_reference")

    col1, col2, col3 = st.columns(3)

    with col1:
        st.write("**Original source file**")
        st.code(case["source_file"], language=None)

    with col2:
        st.write("**Validation reference**")
        st.write(
            str(reference)
            if pd.notna(reference) and str(reference).strip()
            else "Not reported in this field"
        )

    with col3:
        st.write("**GLEIF record**")
        st.markdown(
            "[Reporting entity in GLEIF]"
            f"(https://api.gleif.org/api/v1/"
            f"lei-records/{case['child_lei']})"
        )
        st.caption(
            "The GLEIF link identifies the legal entity. "
            "It is not a direct link to the underlying "
            "validation documents."
        )

    # Find the exact reporting periods for this record; an empty
    # result may have no columns at all.
    period_columns = {"child_lei", "parent_lei", "relationship_type", "snapshot_at_utc"}
    if periods is None or periods.empty or not period_columns <= set(periods.columns):
        periods = pd.DataFrame(columns=sorted(period_columns | {"period_type", "period_start_utc", "period_end_utc"}))
    if "period_source" not in periods.columns:
        periods = periods.assign(period_source="LEGACY_PERIOD_TABLE")

    mask = (
        periods["child_lei"].eq(case["child_lei"])
        & periods["parent_lei"].eq(case["parent_lei"])
        & periods["relationship_type"].eq(
            case["relationship_type"]
        )
        & periods["snapshot_at_utc"].eq(
            case["snapshot_at_utc"]
        )
    )

    expected_source = ("GLEIF_OBSERVATION" if provenance["origin"] == "GLEIF_BATCH"
                       else "LEGACY_PERIOD_TABLE")
    case_periods = periods.loc[mask & periods["period_source"].eq(expected_source)].copy()

    st.subheader("Relationship timeline", anchor="crie-relationship-timeline")

    if case_periods.empty and provenance["origin"] == "GLEIF_BATCH":
        st.info("The GLEIF record for this observation states no periods. That is what "
                "the record says, not evidence that the relationship has no periods.")
    elif case_periods.empty:
        st.info("No period information available.")
    else:
        st.dataframe(
            labels.period_table(case_periods),
            hide_index=True,
            width="stretch"
        )
        st.caption("Relationship, accounting and document filing periods describe different "
                   "facts. An end that is not stated is not evidence that the relationship "
                   "is ongoing.")
        with st.expander("Technical values (as stored)"):
            st.dataframe(
                case_periods[
                    [
                        "period_type",
                        "period_start_utc",
                        "period_end_utc",
                        "period_source",
                    ]
                ],
                hide_index=True,
                width="stretch"
            )

    render_gleif_history(case)

    # Suggest verification steps based on classification
    st.subheader("Suggested investigative actions")

    classification = case["validation_sources"]

    if classification == "ENTITY_SUPPLIED_ONLY":
        actions = (
            "Seek independently accessible corporate filings, "
            "audited consolidated accounts, or official "
            "registry documents that corroborate the "
            "reported relationship."
        )

    elif classification == "PARTIALLY_CORROBORATED":
        actions = (
            "Compare the reported relationship against "
            "the relevant consolidated financial statements "
            "and other dated corporate disclosures. "
            "Record what is established and what remains "
            "unverified."
        )

    else:
        actions = (
            "Determine why the validation classification "
            "is missing or unrecognized before evaluating "
            "the relationship."
        )

    st.info(actions)

    # Export a reproducible review brief
    period_summary = []

    for row in case_periods.itertuples(index=False):
        period_summary.append(
            f"- {row.period_type}: "
            f"{format_date(row.period_start_utc)} to "
            f"{format_date(row.period_end_utc)}"
        )

    report = "\n".join([
        "# Corporate Relationship Evidence Review",
        "",
        f"Company: {case['child_name']}",
        f"Related entity: {case['parent_name']}",
        f"Relationship type: {case['relationship_type']}",
        f"Validation: {case['validation_sources']}",
        f"Documents: {case['validation_documents']}",
        f"Validation reference: "
        f"{reference if pd.notna(reference) and str(reference).strip() else 'Not reported'}",
        f"Data valid as of (GLEIF ContentDate): {format_date(case['snapshot_at_utc'])}",
        f"Last updated: {format_date(case['last_update_at_utc'])}",
        f"Source file: {case['source_file']}",
        f"Evidence row origin: "
        f"{'GLEIF import batch #' + str(provenance['batch_id']) + ' (' + provenance['coverage'] + ')' if provenance['origin'] == 'GLEIF_BATCH' else 'Legacy import'}",
        "",
        "## Recorded periods",
        *period_summary,
        "",
        "## Suggested verification",
        actions,
        "",
        "## Limitations",
        "This is a documentation-review flag, "
        "not a finding of misconduct.",
    ])

    st.download_button(
        "Download evidence review brief",
        data=report,
        file_name="corporate_evidence_review.md",
        mime="text/markdown",
    )
