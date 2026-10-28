
"""
State shared between the dashboard's pages: the selected company, the open
investigation and its section. Widget values do not survive a change of
page, so each is kept under its own session-state key and every widget is
rebuilt from it.

Relationships are identified by a stable key (child LEI, parent LEI and
relationship type), never by their position in a table.
"""

import streamlit as st

import access
import display

COMPANY = "crie_company"                  # LEI of the selected company
CASE = "crie_open_case"                   # permanent case ID of the open investigation
PENDING = "crie_pending_relationship"     # relationship key awaiting a new investigation
SECTION = "crie_open_section"             # open section of the investigation
RELATIONSHIP = "crie_relationship"        # relationship selected in the review queue

PAGES = {}                                # filled by dashboard.py: name -> st.Page


def relationship_key(row):
    return f"{row['child_lei']}|{row['parent_lei']}|{row['relationship_type']}"


def parse_key(key):
    child, parent, kind = key.split("|")
    return child, parent, kind


def keep(state_key, widget_key):
    """An on_change callback storing a widget's value under a lasting key."""

    def store():
        st.session_state[state_key] = st.session_state[widget_key]

    return store


def index_of(options, state_key):
    value = st.session_state.get(state_key)
    return options.index(value) if value in options else None


def cases_by_relationship(cases):
    """Case IDs by relationship key, whichever observation each is anchored on."""

    found = {}
    if cases is None or len(cases) == 0:
        return found
    for row in cases.to_dict("records"):
        if row.get("child_lei"):
            found.setdefault(relationship_key(row), []).append(int(row["case_id"]))
    return found


def open_investigation(case_id=None, relationship=None):
    """Open an existing case, or a relationship awaiting one, on My Investigations."""

    st.session_state[CASE] = case_id
    st.session_state[PENDING] = None if case_id is not None else relationship
    st.session_state.pop(SECTION, None)
    page = PAGES.get("mine")
    if page is not None:
        st.switch_page(page)


def describe(row, cases=None):
    line = (f"{row['child_name']} → {row['parent_name']} "
            f"({display.relationship_type(row['relationship_type'])})")
    ids = (cases or {}).get(relationship_key(row))
    return f"Case #{ids[-1]} · {line}" if ids else line


def investigate_action(row, cases, key):
    """The explicit action for one relationship: open its case, or start one."""

    ids = cases.get(relationship_key(row), [])
    if ids:
        st.write(f"This relationship is investigated in "
                 f"{', '.join(f'Case #{i}' for i in ids)}.")
        if st.button(f"Open Case #{ids[-1]} in My Investigations", key=key, type="primary"):
            open_investigation(case_id=ids[-1])
    else:
        st.write("No investigation has been opened on this relationship.")
        if st.button("Investigate this relationship", key=key, type="primary",
                     **access.start_options()):
            open_investigation(relationship=relationship_key(row))


def relationship_picker(rows, cases, key, label="Relationship"):
    """Choose one of `rows` by stable key, then offer the investigate action."""

    if rows.empty:
        st.info("No relationships to choose from.")
        return
    by_key = {relationship_key(r): r for r in rows.to_dict("records")}
    options = sorted(by_key, key=lambda k: (k not in cases, describe(by_key[k]).lower()))
    choice = st.selectbox(label, options, index=index_of(options, RELATIONSHIP), key=key,
                          placeholder="Choose a relationship",
                          format_func=lambda k: describe(by_key[k], cases),
                          on_change=keep(RELATIONSHIP, key))
    if choice is not None:
        investigate_action(by_key[choice], cases, key=f"{key}_go")
