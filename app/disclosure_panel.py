
import hashlib
import json
from datetime import date

import pandas as pd
import mysql.connector
import streamlit as st

import access
from claim_correction_ui import render_claim_correction_ui
from claim_subject_ui import render_claim_subjects

from src.claim_reconciliation import (
    build_reconciled_comparison,
    reconcile_claims,
)


REQUIRED = ["report_year", "period_end", "source_id", "entity",
            "metric", "value", "source_location"]

# What each status means; shown beside the status, never instead of it.
RECONCILIATION_HELP = {
    "SINGLE_CLAIM": "One recorded claim. Not corroborated.",
    "REPEATED_SOURCE_CLAIMS": "One source records the value more than once. Not corroboration.",
    "DUPLICATE_DOCUMENT_CLAIMS": "The agreeing sources appear to be registrations of one document (same URL, or same title, publisher and date). Not corroboration.",
    "CONSISTENT_RELATED_SOURCES": "Sources not recognised as one document agree but share a publisher or a recorded dependence. Not independent corroboration.",
    "INDEPENDENCE_UNVERIFIED": "Sources not recognised as one document agree; their independence is not established. Not corroboration.",
    "CONSISTENT_SOURCES": "Documents with recorded separate origin agree: independent corroboration.",
    "UNRESOLVED_CONFLICT": "Recorded values disagree. No value is selected.",
}

COMPARISON_HELP = {
    "UNCHANGED": "Equal values in compatible periods.",
    "CHANGED": "Different values in compatible periods.",
    "PERIOD_MISMATCH": "Reporting periods are not comparable; no change is computed.",
    "MISSING_PERIOD": "No claim for one of the two years.",
    "UNRESOLVED_CONFLICT": "A year contains conflicting claims; no change is computed.",
}

AGREEMENT = {"REPEATED_SOURCE_CLAIMS", "DUPLICATE_DOCUMENT_CLAIMS",
             "CONSISTENT_RELATED_SOURCES", "INDEPENDENCE_UNVERIFIED"}


def analyze_case_claims(records):
    """
    Reconcile and compare a case's current claims (pure).

    Claims missing any required field, including the reporting
    period, are separated first: before any text conversion, so a
    missing value can never be grouped with others as "None". The
    remaining claims are reconciled with their sources' metadata, and
    the two latest years are compared.
    """

    df = pd.DataFrame(records)
    result = {"incomplete": df.iloc[0:0], "reconciliation": None,
              "comparison": None, "years": None, "error": None}

    if df.empty:
        return result

    missing = df[REQUIRED].isna()
    for column in REQUIRED:
        missing[column] |= df[column].astype(object).map(
            lambda v: isinstance(v, str) and not v.strip())

    result["incomplete"] = df[missing.any(axis=1)]
    complete = df[~missing.any(axis=1)]

    if complete.empty:
        return result

    claims = complete[["claim_id", *REQUIRED]].astype(str)
    sources = (
        complete[["source_id", "source_url", "publisher", "document_title",
                  "publication_date"]]
        .drop_duplicates("source_id")
        .to_dict("records")
    )

    try:
        result["reconciliation"] = reconcile_claims(claims, sources=sources)

        years = sorted(claims["report_year"].unique(), key=int)
        if len(years) >= 2:
            previous, current = years[-2], years[-1]
            pair = claims[claims["report_year"].isin([previous, current])]
            result["comparison"] = build_reconciled_comparison(
                pair, previous_year=previous, current_year=current,
                sources=sources,
            )
            result["years"] = (previous, current)
    except ValueError as error:
        result["error"] = str(error)

    return result


def claim_fingerprint(source_id, entity, metric, year, period_end, value, location):
    """The claim's fingerprint, as src/import_disclosure_claims.py computes it."""

    return hashlib.sha256(json.dumps(
        [source_id, entity, metric, year, period_end.isoformat(), value, location],
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def record_claim(connect_db, case_id, source_id, entity, metric, year, period_end, value,
                 location):
    """Record one claim of a registered source of the case; returns its claim ID."""

    from src.comparison_rules import normalize_claim

    normalized = normalize_claim(metric, value)       # a malformed percentage is refused here
    connection = connect_db()
    cursor = connection.cursor()
    try:
        access.require_write(connection, case_id)
        cursor.execute("SELECT 1 FROM case_sources WHERE source_id = %s AND case_id = %s",
                       (source_id, case_id))
        if cursor.fetchone() is None:
            raise ValueError("The source is not registered for this investigation.")
        cursor.execute(
            "INSERT INTO disclosure_claims (source_id, entity_name, metric, report_year, "
            "period_end, value_text, normalized_value, source_location, claim_fingerprint) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (source_id, entity, metric, year, period_end, value, normalized, location,
             claim_fingerprint(source_id, entity, metric, year, period_end, value, location)))
        connection.commit()
        return cursor.lastrowid
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def _case_sources(connect_db, case_id):
    connection = connect_db()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT source_id, document_title FROM case_sources "
                       "WHERE case_id = %s ORDER BY source_id", (case_id,))
        return cursor.fetchall()
    finally:
        cursor.close()
        connection.rollback()
        connection.close()


def render_claim_entry(case_id, connect_db):
    """Record what a registered source states, with where it states it."""

    with st.expander("Record a claim from a registered source"):
        try:
            sources = _case_sources(connect_db, case_id)
        except Exception as error:  # noqa: BLE001 - shown, never hidden
            st.error(f"Sources could not be loaded: {error}")
            return
        if not sources:
            st.info("Register a source first, under Sources and assessments.")
            return
        st.caption("Transcribe one value exactly as the source states it. A recorded claim is "
                   "never edited; a mistake is fixed later with a correction, which keeps "
                   "both versions. Metrics ending in _pct are read as percentages.")
        titles = {int(s["source_id"]): s["document_title"] for s in sources}
        with st.form(f"claim_entry_{case_id}"):
            source_id = st.selectbox("Source", list(titles),
                                     format_func=lambda i: f"S{i}: {titles[i]}")
            entity = st.text_input("Entity, exactly as the source names it")
            metric = st.text_input("Metric", placeholder="e.g. parent_interest_pct")
            col1, col2 = st.columns(2)
            year = col1.number_input("Report year", min_value=1900, max_value=2100,
                                     value=date.today().year - 1, step=1)
            period_text = col2.text_input("Period end", placeholder="YYYY-MM-DD")
            value = st.text_input("Value, as stated")
            location = st.text_input("Location in the source", placeholder="Page, note or section")
            access.notice()
            submitted = st.form_submit_button("Record claim", **access.submit_options())
        if not submitted:
            return
        fields = [entity.strip(), metric.strip(), value.strip(), location.strip()]
        if not all(fields) or not period_text.strip():
            st.error("Every field is required.")
            return
        try:
            period_end = date.fromisoformat(period_text.strip())
        except ValueError:
            st.error("The period end must use YYYY-MM-DD.")
            return
        if " " in metric.strip():
            st.error("Use a metric name without spaces, such as parent_interest_pct.")
            return
        try:
            claim_id = record_claim(connect_db, case_id, source_id, fields[0], fields[1],
                                    int(year), period_end, fields[2], fields[3])
        except mysql.connector.IntegrityError:
            st.error("This exact claim is already recorded.")
        except Exception as error:  # noqa: BLE001 - shown, never hidden
            st.error(f"The claim was not recorded: {error}")
        else:
            st.success(f"Claim #{claim_id} recorded.")


def render_disclosure_panel(case_id, connect_db):
    """Display registered claims and conflict analysis."""

    st.divider()
    st.subheader("Claims and identities", anchor="crie-claims")
    render_claim_entry(case_id, connect_db)

    # Retrieve claims belonging to this investigation.
    connection = None
    cursor = None

    try:
        connection = connect_db()
        cursor = connection.cursor(dictionary=True)

        cursor.execute(
            """
            SELECT
                c.claim_id,
                c.source_id,
                c.entity_name AS entity,
                c.metric,
                c.report_year,
                c.period_end,
                c.value_text AS value,
                c.source_location,
                s.document_title,
                s.source_url,
                s.publisher,
                s.publication_date,
                s.evidence_assessment
            FROM v_current_disclosure_claims c
            JOIN case_sources s
                ON c.source_id = s.source_id
            WHERE s.case_id = %s
            ORDER BY
                c.report_year,
                c.metric,
                c.claim_id
            """,
            (case_id,),
        )

        records = cursor.fetchall()

    except Exception as error:
        st.error(f"Could not retrieve disclosure claims: {error}")
        return

    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()

    if not records:
        st.info("No disclosure claims registered for this case.")
        return

    df = pd.DataFrame(records)

    analysis = analyze_case_claims(records)
    reconciliation = analysis["reconciliation"]

    def count(statuses):
        if reconciliation is None:
            return 0
        return int(reconciliation["reconciliation_status"].isin(statuses).sum())

    flagged = count({"UNRESOLVED_CONFLICT"})

    # Summary metrics
    col1, col2, col3 = st.columns(3)

    col1.metric("Registered claims", len(df))
    col2.metric("Registered sources", df["source_id"].nunique())
    col3.metric(
        "Conflicting disclosures",
        flagged if reconciliation is not None else "N/A",
    )

    tab1, tab2 = st.tabs(
        ["Registered Claims", "Conflict Audit"]
    )

    # TAB 1: Individual source-linked claims
    with tab1:
        st.caption(
            "Each row represents a recorded disclosure "
            "linked to its original source document."
        )

        display = df[
            [
                "claim_id",
                "entity",
                "metric",
                "report_year",
                "period_end",
                "value",
                "source_id",
                "document_title",
                "source_location",
                "source_url",
                "evidence_assessment",
            ]
        ].copy()

        display["period_end"] = display["period_end"].map(
            lambda v: "Not recorded" if v is None or pd.isna(v) else str(v)
        )

        display = display.rename(
            columns={
                "claim_id": "Claim ID",
                "entity": "Entity",
                "metric": "Metric",
                "report_year": "Year",
                "period_end": "Period End",
                "value": "Reported Value",
                "source_id": "Source ID",
                "document_title": "Document",
                "source_location": "Location",
                "source_url": "Source URL",
                "evidence_assessment": "Source Assessment",
            }
        )

        st.dataframe(
            display,
            hide_index=True,
            width="stretch",
            column_config={
                "Source URL": st.column_config.LinkColumn(
                    "Source URL",
                    display_text="Open document",
                ),
            },
        )

        st.caption(
            "Source assessments apply to the registered "
            "documents; they are not independent "
            "verification of each claim."
        )

    # TAB 2: Analytical findings
    with tab2:
        _render_analysis(analysis, count)

    # Identity of the claims' subjects, then their corrections, follow the claims.
    render_claim_subjects(
        case_id,
        connect_db,
    )
    render_claim_correction_ui(
        case_id,
        connect_db,
    )


def _render_analysis(analysis, count):
    incomplete = analysis["incomplete"]
    reconciliation = analysis["reconciliation"]

    if not incomplete.empty:
        st.warning(
            f"{len(incomplete)} claim(s) lack a required field, such as "
            "the reporting period. They are excluded from the analysis "
            "below and cannot count as consistent or corroborated: "
            + ", ".join(f"#{int(i)}" for i in incomplete["claim_id"])
        )

    if analysis["error"]:
        st.error(f"Reconciliation could not run: {analysis['error']}")
        return

    if reconciliation is None:
        st.info("No complete claims are available for analysis.")
        return

    st.caption(
        "Claims are grouped by entity, metric, reporting year, and "
        "period-end date. Agreement between sources is corroboration "
        "only when their separate origin is recorded."
    )

    st.markdown(
        f"- **Single claims (not corroborated):** {count({'SINGLE_CLAIM'})}\n"
        f"- **Agreement without established independence:** {count(AGREEMENT)}\n"
        f"- **Independently corroborated:** {count({'CONSISTENT_SOURCES'})}\n"
        f"- **Conflicting evidence:** {count({'UNRESOLVED_CONFLICT'})}"
    )

    summary = reconciliation[
        ["entity", "metric", "report_year", "period_end", "claim_count",
         "source_count", "reconciliation_status"]
    ].copy()
    summary["meaning"] = summary["reconciliation_status"].map(RECONCILIATION_HELP)
    summary = summary.rename(columns={
        "entity": "Entity", "metric": "Metric", "report_year": "Year",
        "period_end": "Period End", "claim_count": "Claims",
        "source_count": "Registered sources", "reconciliation_status": "Status",
        "meaning": "Meaning",
    })
    st.dataframe(summary, hide_index=True, width="stretch")

    comparison = analysis["comparison"]
    if comparison is not None:
        previous, current = analysis["years"]
        st.markdown(f"**{previous} → {current} comparison**")
        table = comparison[["entity", "metric", "comparison_status",
                            "period_detail"]].copy()
        table["meaning"] = table["comparison_status"].map(COMPARISON_HELP)
        st.dataframe(
            table.rename(columns={
                "entity": "Entity", "metric": "Metric",
                "comparison_status": "Status", "period_detail": "Period detail",
                "meaning": "Meaning",
            }),
            hide_index=True,
            width="stretch",
        )

    conflicts = reconciliation[
        reconciliation["reconciliation_status"] == "UNRESOLVED_CONFLICT"
    ]
    for _, row in conflicts.iterrows():
        with st.expander(f"{row['entity']} — {row['metric']} ({row['report_year']})"):
            st.write("Scope:", row["conflict_scope"])
            for claim in json.loads(row["claim_details"]):
                st.write(
                    f"Source #{claim['source_id']}: {claim['value']} "
                    f"({claim['source_location']})"
                )

    st.caption(
        "Differences are review flags, not findings of misrepresentation "
        "or fraud. Agreement is not proof that a statement is true."
    )
