
import os
import sys
import textwrap
from pathlib import Path

# `streamlit run app/dashboard.py` puts only app/ on the import path; the
# repository root is needed for src/ (and app.* imports).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mysql.connector
import networkx as nx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv
from review_queue import render_review_queue

import access
import display
import investigations
import navigation
from case_management import connect_db
from glossary import render_glossary, render_terms
from published import render_published_page
from src.relationship_context import load_relationship_context

ROOT = Path(__file__).resolve().parents[1]
# Locally .env wins; a hosted installation uses only the platform's variables.
load_dotenv(ROOT / ".env", override=os.getenv("CRIE_HOSTED") != "1")

PRODUCTION_DATABASE = "corporate_intelligence"

st.set_page_config(
    page_title="Corporate Relationship Intelligence",
    page_icon="🔎",
    layout="wide",
)


def connect():
    access.require_hosted_target()
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE"),
    )


@st.cache_data(ttl=300)
def ownership_installed():
    connection = connect()
    try:
        return access.ownership_installed(connection)
    finally:
        connection.rollback()
        connection.close()


@st.cache_data(ttl=300)
def load_data():
    connection = connect()

    try:
        # Legacy queries unless the 23B.2B observation schema is present;
        # then only completed batches' anchors count (see
        # src/relationship_context.py).
        relationships, periods, _ = load_relationship_context(connection)
    finally:
        connection.close()

    relationships = pd.DataFrame(relationships)
    periods = pd.DataFrame(periods)

    return relationships, periods


def build_graph(relationships):
    graph = nx.DiGraph()

    for row in relationships.itertuples(index=False):
        graph.add_node(
            row.child_lei,
            name=row.child_name,
            jurisdiction=row.child_jurisdiction,
        )

        graph.add_node(
            row.parent_lei,
            name=row.parent_name,
            jurisdiction=row.parent_jurisdiction,
        )

        if not graph.has_edge(row.child_lei, row.parent_lei):
            graph.add_edge(
                row.child_lei,
                row.parent_lei,
                types=set(),
            )

        graph[row.child_lei][row.parent_lei]["types"].add(
            row.relationship_type
        )

    return graph


# Edge styles by reported relationship type; colours read on light and dark.
DIRECT, ULTIMATE = "IS_DIRECTLY_CONSOLIDATED_BY", "IS_ULTIMATELY_CONSOLIDATED_BY"
EDGE_STYLES = {
    DIRECT: {"color": "#3B82F6", "dash": "solid"},
    ULTIMATE: {"color": "#D97706", "dash": "dash"},
}
OTHER_EDGE = {"color": "#94A3B8", "dash": "dot"}
SELECTED, NODE = "#2563EB", "#94A3B8"
LABEL_LIMIT = 12          # above this, only the selected entity and its parents are labelled
EDGE_LABEL_LIMIT = 8      # above this, relationship types are shown by colour, legend and hover


def layered_positions(graph):
    """
    Consolidating parents above the entities that report them. Each node's
    level is its longest path to a top parent; falls back to a force layout
    for cycles or very large networks.
    """

    if not nx.is_directed_acyclic_graph(graph) or len(graph) > 60:
        return nx.spring_layout(graph, seed=42), None

    level = {}
    for node in reversed(list(nx.topological_sort(graph))):
        parents = list(graph.successors(node))
        level[node] = 1 + max(level[p] for p in parents) if parents else 0

    rows = {}
    for node, depth in level.items():
        rows.setdefault(depth, []).append(node)

    positions = {}
    for depth, nodes in rows.items():
        nodes.sort(key=lambda n: graph.nodes[n]["name"].lower())
        for i, node in enumerate(nodes):
            positions[node] = ((i + 1) / (len(nodes) + 1), -depth)
    return positions, max(rows) + 1


def edge_style(types):
    for kind in (DIRECT, ULTIMATE):
        if kind in types:
            return EDGE_STYLES[kind]
    return OTHER_EDGE


def draw_network(graph, selected_lei):
    positions, levels = layered_positions(graph)
    labelled = (set(graph) if len(graph) <= LABEL_LIMIT
                else {selected_lei, *graph.successors(selected_lei)})
    edge_labels = graph.number_of_edges() <= EDGE_LABEL_LIMIT
    hover_x, hover_y, hover_text = [], [], []

    fig = go.Figure()

    for child, parent, data in graph.edges(data=True):
        x0, y0 = positions[child]
        x1, y1 = positions[parent]
        style = edge_style(data["types"])

        fig.add_trace(go.Scatter(
            x=[x0, x1],
            y=[y0, y1],
            mode="lines",
            line=dict(color=style["color"], width=2.4, dash=style["dash"]),
            hoverinfo="skip",
            showlegend=False,
        ))

        # Arrow from the reporting entity to its consolidating parent.
        fig.add_annotation(
            x=x1, y=y1,
            ax=x0, ay=y0,
            xref="x", yref="y",
            axref="x", ayref="y",
            text="",
            showarrow=True,
            arrowhead=3,
            arrowsize=1.3,
            arrowwidth=2,
            arrowcolor=style["color"],
            standoff=16,
        )

        kinds = " and ".join(display.relationship_type(t) for t in sorted(data["types"]))
        hover_x.append((x0 + x1) / 2)
        hover_y.append((y0 + y1) / 2)
        hover_text.append(f"{graph.nodes[child]['name']}<br><b>{kinds}</b><br>"
                          f"{graph.nodes[parent]['name']}")
        if not edge_labels:
            continue

        # The label sits beside the line, on the side the line leans towards.
        leans_left = x1 < x0
        fig.add_annotation(
            x=(x0 + x1) / 2,
            y=(y0 + y1) / 2,
            text=kinds,
            showarrow=False,
            font=dict(size=12, color=style["color"]),
            xanchor="right" if leans_left else "left",
            xshift=-14 if leans_left else 14,
        )

    # Hover targets at each relationship's midpoint.
    fig.add_trace(go.Scatter(
        x=hover_x, y=hover_y, mode="markers", marker=dict(size=14, opacity=0),
        hovertext=hover_text, hoverinfo="text", showlegend=False,
    ))

    # Legend entries for the relationship types present.
    present = {t for _, _, d in graph.edges(data=True) for t in d["types"]}
    for kind, style in EDGE_STYLES.items():
        if kind in present:
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="lines",
                line=dict(color=style["color"], width=2.4, dash=style["dash"]),
                name=display.relationship_type(kind),
            ))
    fig.add_trace(go.Scatter(
        x=[None], y=[None], mode="markers",
        marker=dict(size=14, color=SELECTED, line=dict(color="#FFFFFF", width=2)),
        name="Selected company",
    ))

    for lei, attributes in graph.nodes(data=True):
        x, y = positions[lei]
        name = attributes["name"]
        chosen = lei == selected_lei

        fig.add_trace(go.Scatter(
            x=[x],
            y=[y],
            mode="markers+text" if lei in labelled else "markers",
            text=["<br>".join(textwrap.wrap(name, width=26))],
            # Entities that report a parent are labelled below, clear of their arrows.
            textposition="bottom center" if graph.out_degree(lei) else "top center",
            textfont=dict(size=14 if chosen else 13),
            marker=dict(
                size=30 if chosen else 22,
                color=SELECTED if chosen else NODE,
                line=dict(color="#FFFFFF", width=3 if chosen else 2),
            ),
            hovertext=(
                f"<b>{name}</b><br>"
                f"LEI: {lei}<br>"
                f"Jurisdiction: {attributes['jurisdiction'] or 'Not stated'}"
            ),
            hoverinfo="text",
            showlegend=False,
        ))

    xs = [p[0] for p in positions.values()]
    ys = [p[1] for p in positions.values()]
    xpad = max((max(xs) - min(xs)) * 0.15, 0.2)
    ypad = max((max(ys) - min(ys)) * 0.12, 0.35)
    height = min(max(160 * levels + 180, 380), 760) if levels else 600

    fig.update_layout(
        height=height,
        margin=dict(l=10, r=10, t=10, b=10),
        xaxis=dict(visible=False, range=[min(xs) - xpad, max(xs) + xpad]),
        yaxis=dict(visible=False, range=[min(ys) - ypad * 1.6, max(ys) + ypad * 1.6]),
        plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="bottom", y=0, xanchor="left", x=0),
        hoverlabel=dict(align="left"),
    )

    return fig


database = os.getenv("MYSQL_DATABASE") or "not configured"

try:
    relationships, periods = load_data()
    st.session_state[access.OWNERSHIP] = ownership_installed()
except access.HostingConfigurationError as error:
    st.error(f"CRIE is not available: {error}")
    st.stop()
except mysql.connector.Error as error:
    st.error(f"Database connection failed: {error}")
    st.stop()

access.keep_guest_link()

if access.hosted() and not st.session_state[access.OWNERSHIP]:
    st.error("CRIE is not available: a hosted installation needs the investigation "
             "ownership schema.")
    st.stop()

if relationships.empty:
    st.warning("No corporate relationships found.")
    st.stop()

try:
    cases = investigations.load_cases(connect_db)
except mysql.connector.Error:
    cases = pd.DataFrame()

graph = build_graph(relationships)

components = list(nx.weakly_connected_components(graph))

options = sorted(
    graph.nodes,
    key=lambda lei: graph.nodes[lei]["name"].lower(),
)

# The selected company carries across every page.
selected_lei = st.session_state.get(navigation.COMPANY)
if selected_lei not in graph:
    selected_lei = None

if selected_lei is not None:
    selected_group = next(
        group for group in components
        if selected_lei in group
    )
    subgraph = graph.subgraph(selected_group).copy()
    group_records = relationships[
        relationships["child_lei"].isin(selected_group)
        & relationships["parent_lei"].isin(selected_group)
    ].copy()
    company_name = graph.nodes[selected_lei]["name"]
else:
    selected_group, subgraph, group_records, company_name = None, None, None, None

NO_COMPANY = "Search for a company above to see its reported network."

with st.sidebar:
    st.markdown("**Corporate Relationship Intelligence**")
    user = access.current_user()
    if access.is_guest(user):
        st.write("**Your private sandbox**")
        st.caption("Your investigations here are private to this page's link: bookmark it to "
                   "come back, and share it only with people who may see and change them.")
    elif user is not None:
        st.write(f"Signed in as **{user.name}**")
        if user.subject.startswith("oidc:") and st.button("Sign out", key="crie_sign_out"):
            st.logout()
    elif st.session_state[access.OWNERSHIP]:
        st.caption("You can read published investigations"
                   + ("; start a sandbox to try your own." if access.sandbox()
                      else "; sign in to create your own."))
        if access.sign_in_available() and st.button("Sign in", key="crie_sign_in"):
            st.login()
    render_glossary()


def investigate_page():
    st.title("Corporate Relationship Intelligence")
    st.markdown(
        "An evidence-grounded corporate intelligence platform for investigating entity "
        "relationships, analyzing historical disclosures, evaluating source independence, "
        "and producing traceable investigative reports."
    )

    if access.sandbox():
        render_try_panel()

    st.selectbox(
        "Search for a company",
        options=options,
        index=navigation.index_of(options, navigation.COMPANY),
        placeholder="Type a company name or LEI",
        format_func=lambda lei: (
            f"{graph.nodes[lei]['name']} ({lei})"
        ),
        key="crie_selected_company",
        on_change=navigation.keep(navigation.COMPANY, "crie_selected_company"),
    )
    st.caption(
        f"{len(options)} entities and {len(relationships)} reported relationships are loaded "
        "from GLEIF. Choose a company to see its reported consolidation network, then a "
        "relationship to investigate."
    )

    if selected_lei is not None:
        st.markdown(f"#### Selected network: {company_name}")
        c1, c2, c3 = st.columns(3)

        c1.metric("Entities in this network", len(selected_group),
                  help="Entities connected to the selected company by reported relationships.")
        c2.metric("Reported relationships in this network", len(group_records))
        c3.metric(
            "Outside GLEIF's fully corroborated class",
            int(
                group_records["validation_sources"]
                .ne("FULLY_CORROBORATED").sum()
            ),
            help=(
                "Relationships in this network whose GLEIF validation class is not "
                "FULLY_CORROBORATED. That is GLEIF's own classification, not a CRIE "
                "corroboration finding, and it does not indicate wrongdoing."
            ),
        )

        st.plotly_chart(
            draw_network(subgraph, selected_lei),
            width="stretch",
        )

        st.caption(
            "Arrows point from a reporting entity to its reported consolidating parent. "
            "Accounting consolidation is reported for financial reporting; it is not "
            "evidence of direct share ownership or managerial control. This network "
            "shows only the relationships loaded into CRIE, not a complete ownership "
            "tree."
            + (" Hover over a node for its name and LEI, or over a line for its "
               "relationship." if len(subgraph) > LABEL_LIMIT else "")
        )

        st.markdown(
            f"[Open the selected entity's GLEIF record]"
            f"(https://api.gleif.org/api/v1/lei-records/{selected_lei})"
        )

        with st.container(border=True):
            st.markdown("**Investigate a relationship in this network**")
            navigation.relationship_picker(
                group_records, navigation.cases_by_relationship(cases),
                key="crie_explore_relationship")
    else:
        st.info(NO_COMPANY)

    evidence_tab, timeline_tab, leads_tab = st.tabs(
        [
            "Evidence",
            "Timeline",
            "Research leads",
        ]
    )

    with evidence_tab:
        st.subheader("Relationship evidence")

        if selected_lei is None:
            st.info(NO_COMPANY)
        else:
            st.dataframe(
                display.relationship_table(group_records),
                hide_index=True,
                width="stretch",
            )

            review = group_records[
                group_records["validation_sources"].ne(
                    "FULLY_CORROBORATED"
                )
            ]

            if not review.empty:
                st.warning(
                    f"{len(review)} relationship record(s) have "
                    "a validation classification requiring "
                    "closer documentation review."
                )

            st.caption(
                "The GLEIF validation class describes how GLEIF validated the record. It "
                "is not CRIE's corroboration assessment, and a lower class does not "
                "establish misconduct. Data valid as of is each GLEIF file's ContentDate."
            )

            with st.expander("Technical values (as stored)"):
                st.dataframe(
                    group_records[
                        [
                            "child_lei",
                            "parent_lei",
                            "relationship_type",
                            "validation_sources",
                            "validation_documents",
                            "validation_reference",
                            "last_update_at_utc",
                            "snapshot_at_utc",
                            "source_file",
                        ]
                    ],
                    hide_index=True,
                    width="stretch",
                )

            st.download_button(
                "Export network evidence as CSV",
                data=group_records.to_csv(index=False),
                file_name="network_evidence.csv",
                mime="text/csv",
            )

    with timeline_tab:
        st.subheader("Recorded relationship and document periods")

        if selected_lei is None:
            st.info(NO_COMPANY)
        elif periods.empty:
            st.info("No period records available.")
        else:
            period_keys = set(
                zip(
                    group_records["child_lei"],
                    group_records["parent_lei"],
                    group_records["relationship_type"],
                    group_records["snapshot_at_utc"],
                )
            )

            keys = list(
                periods[
                    [
                        "child_lei",
                        "parent_lei",
                        "relationship_type",
                        "snapshot_at_utc",
                    ]
                ].itertuples(index=False, name=None)
            )

            group_periods = periods[
                [key in period_keys for key in keys]
            ].copy()

            names = {lei: graph.nodes[lei]["name"] for lei in selected_group}
            st.dataframe(
                display.period_table(group_periods, names),
                hide_index=True,
                width="stretch",
            )

            st.caption(
                "Relationship periods, accounting periods, and "
                "document filing periods describe different facts. "
                "Blank end dates are not automatically ongoing "
                "relationships."
            )

    with leads_tab:
        st.caption("Research leads: relationships whose GLEIF validation class is not "
                   "FULLY_CORROBORATED, across the whole dataset or the selected network. "
                   "A lead is a place to start looking, not a finding.")
        render_review_queue(relationships, periods, cases, selected_group, company_name)


def render_try_panel():
    """How a visitor tries CRIE: read the example, then investigate in a private sandbox."""

    with st.container(border=True):
        st.markdown("**Try CRIE**")
        col1, col2 = st.columns(2)
        with col1:
            st.write("**1. Read a published investigation.** The synthetic example shows "
                     "every step, from GLEIF records to a verified report.")
            st.page_link(navigation.PAGES["published"], label="Open Published Investigations",
                         icon=":material/public:")
        with col2:
            if access.is_guest(access.current_user()):
                st.write("**2. Your sandbox is ready.** Search for a company below, choose a "
                         "relationship and click **Investigate this relationship**.")
            else:
                st.write("**2. Investigate yourself** in a private sandbox: no account; "
                         "private to whoever holds its link.")
                if st.button("Start a private sandbox", type="primary", key="crie_start_sandbox"):
                    access.start_guest()
                    st.rerun()


def mine_page():
    investigations.render_investigations(relationships, periods, cases, selected_group,
                                         company_name, connect_db)


def published_page():
    render_published_page(connect_db)


def methodology_page():
    st.title("Methodology")
    st.markdown(
        "CRIE records what an investigator found and how, so that every conclusion can be "
        "traced to its evidence and checked later. It decides nothing automatically: "
        "identity, source assessments and the case assessment are recorded by people, with "
        "their reasoning."
    )

    st.subheader("Evidence rules")
    st.markdown(
        "- **Consolidation is not ownership.** A GLEIF accounting-consolidation "
        "relationship means the entity is reported as consolidated in the parent's "
        "financial statements; it is not evidence of direct ownership or managerial "
        "control.\n"
        "- **GLEIF classes are GLEIF's.** Validation classes such as "
        "`FULLY_CORROBORATED` describe how GLEIF validated a record. CRIE never treats "
        "them, or any GLEIF record, as independent corroboration of a claim.\n"
        "- **Source independence.** Agreement counts as corroboration only between sources "
        "whose independent origin an investigator has recorded. Republished, related or "
        "unassessed sources never corroborate one another.\n"
        "- **Historical comparison.** Each GLEIF file is kept as an observation and compared "
        "field by field. A difference between published records, or a relationship missing "
        "from a later file, does not establish that, or when, anything changed. A file's "
        "ContentDate is when its data is valid as of, not the date of an event.\n"
        "- **Provenance and quarantine.** Every GLEIF row records the file and import batch "
        "it came from. Records with invalid identifiers are quarantined and never become "
        "trusted entities or relationships.\n"
        "- **Corrections.** A stored claim is never edited. A correction or restoration adds "
        "a new version with its reason and evidence; earlier versions stay recorded, as do "
        "earlier assessments and identity mappings.\n"
        "- **Snapshot integrity.** A report is rendered from one consistent evidence "
        "snapshot and registered with the hashes of its files. INTACT means the files match "
        "those hashes, not that the disclosures are correct; STALE means only that the "
        "evidence has changed since."
    )

    st.subheader("Investigations, drafts and publication")
    st.markdown(
        "Each investigation belongs to the user who opened it and is anchored on the GLEIF "
        "evidence row it was opened on. Drafts are visible only to their owner. Publishing "
        "makes one verified report, the author's title and the author's summary readable by "
        "everyone; later work stays private, and every earlier publication stays recorded."
    )

    st.subheader("Architecture")
    st.markdown(
        "- **`src/`:** GLEIF ingestion (a hardened streaming parser, restartable batches, "
        "quarantine), historical comparison, source independence, claim lineage, identity "
        "mappings, reconciliation, and snapshot-driven reports with a registry.\n"
        "- **`app/`:** this Streamlit interface.\n"
        "- **MySQL:** the system of record. Append-only tables and triggers enforce the "
        "audit rules in the database itself."
    )

    st.subheader("Glossary")
    render_terms()


navigation.PAGES.update({
    "investigate": st.Page(investigate_page, title="Investigate", icon=":material/search:",
                           url_path="investigate", default=True),
    "mine": st.Page(mine_page, title="My Investigations", icon=":material/folder_open:",
                    url_path="my-investigations"),
    "published": st.Page(published_page, title="Published Investigations",
                         icon=":material/public:", url_path="published"),
    "methodology": st.Page(methodology_page, title="Methodology",
                           icon=":material/menu_book:", url_path="methodology"),
})
page = st.navigation(list(navigation.PAGES.values()), position="top")

if database != PRODUCTION_DATABASE and not access.hosted():
    st.warning(
        f"**Demonstration database (`{database}`).** It holds synthetic data for "
        "demonstration and testing, not investigative work."
    )

page.run()

st.divider()

st.caption(
    "Sources: GLEIF relationship (RR-CDF) and entity (LEI-CDF) records loaded into this "
    "database, and the documents investigators register. Coverage is limited to that "
    "data. Reported consolidation relationships are not established ownership or "
    "control, and every finding rests on the recorded evidence and its stated limitations."
)
