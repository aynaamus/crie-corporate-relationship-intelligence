
"""
Investigator views of GLEIF source history (Milestone 23B.2B-3d). They
display what src/relationship_history.py returns and reuse its
classifications; nothing here reinterprets a record, writes, or changes
an identity mapping.
"""

import json

import pandas as pd
import streamlit as st

from display import HISTORY_FIELDS, ORIGINS, gleif_validation, label, readable, utc
from src.relationship_history import entity_history, parent_history, relationship_history

GLOBAL_CAPTION = (
    "Global GLEIF source information, the same for every investigation: not case "
    "evidence, and not independent corroboration of any claim. Dates are each file's "
    "data-valid-as-of time (GLEIF ContentDate), not the date of any corporate event."
)
STATUS_TEXT = {
    "CONSISTENT": "Consistent across the published records",
    "DIFFERS": "The published records differ (not a verified corporate change)",
    "SINGLE_OBSERVATION": "Stated in one published record only",
    "NOT_STATED": "Not stated in any published record",
    "NOT_CAPTURED": "Not captured (legacy import)",
}


def _show(value):
    """A stored value as text; None stays visibly unknown."""

    if value is None:
        return "Not stated"
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) >= {"text"}:
        return value["text"] + (f" [{value['lang']}]" if value.get("lang") else "")
    if isinstance(value, list) and value and isinstance(value[0], dict) and "type" in value[0] \
            and "start" in value[0]:
        return "; ".join(f"{p['type']}: {p['start'] or 'start not stated'} to "
                         f"{p['end'] or 'end not stated'}" for p in value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _coverage(entry):
    batches = entry.get("covering_batches") or ()
    return ", ".join(sorted({b["coverage"] for b in batches})) or "Legacy import"


def relationship_rows(history):
    """One row per published (or legacy) entry, oldest data first."""

    return [{
        "Data valid as of": entry["data_valid_as_of_utc"],
        "Source": entry["source"],
        "Origin": entry["origin"],
        "Coverage": _coverage(entry),
        "Relationship status": _show(entry["values"]["relationship_status"]),
        "Registration status": _show(entry["values"]["registration_status"]),
        "Validation": _show(entry["values"]["validation_sources"]),
        "Reported periods": _show(entry["values"]["periods"]),
    } for entry in history["entries"]]


def entity_rows(history):
    return [{
        "Data valid as of": entry["data_valid_as_of_utc"],
        "Source": entry["source"],
        "Coverage": _coverage(entry),
        "Legal name": _show(entry["values"]["legal_name"]),
        "Entity status": _show(entry["values"]["entity_status"]),
        "Jurisdiction": _show(entry["values"]["legal_jurisdiction"]),
        "Registration status": _show(entry["values"]["registration_status"]),
        "Flagged identifiers": ", ".join(f["role"] for f in entry.get("identifier_flags") or ()) or "None",
    } for entry in history["entries"]]


def comparison_rows(comparison):
    rows = []
    for field, result in comparison.items():
        detail = ""
        if result["status"] == "DIFFERS":
            detail = "; ".join(f"{_show(d['before'])} → {_show(d['after'])} between {d['from']} and "
                               f"{d['to']}" + (" (same data-valid-as-of time)" if d["same_valid_as_of"]
                                               else "") for d in result["differences"])
        elif result.get("values"):
            detail = _show(result["values"][0])
        rows.append({"Field": field, "Result": STATUS_TEXT[result["status"]], "Detail": detail or "—"})
    return rows


def name_rows(lei, history):
    """Every name a published record states for an LEI, with its source."""

    rows = []
    for entry in history["entries"]:
        values = entry["values"]
        names = [("LEGAL_NAME", values["legal_name"])]
        names += [(n["type"], n) for n in values["other_entity_names"] or ()]
        names += [(n["type"], n) for n in values["transliterated_other_entity_names"] or ()]
        for kind, name in names:
            rows.append({"LEI": lei, "Name": name["text"], "Name type": kind,
                         "Language": name.get("lang") or "Not stated",
                         "Data valid as of": entry["data_valid_as_of_utc"],
                         "Source": entry["source"]})
    return rows


def _valid_as_of(value):
    """A data-valid-as-of time in the common UTC form; anything else unchanged."""

    try:
        return utc(value) if value else value
    except (TypeError, ValueError):
        return value


def _observations(rows):
    """Observation rows with readable origin, coverage and validation labels."""

    table = pd.DataFrame(rows)
    if table.empty:
        return table
    table["Data valid as of"] = table["Data valid as of"].map(_valid_as_of)
    if "Origin" in table:
        table["Origin"] = table["Origin"].map(lambda o: label(ORIGINS, o))
    table["Coverage"] = table["Coverage"].map(
        lambda c: ", ".join(readable(part) for part in c.split(", ")))
    if "Validation" in table:
        table = table.rename(columns={"Validation": "GLEIF validation class"})
        table["GLEIF validation class"] = table["GLEIF validation class"].map(
            lambda v: v if v in ("Not stated", "NOT CAPTURED") else gleif_validation(v))
    return table


def _comparison(rows):
    """Comparison rows with readable field names; the stored name stays alongside."""

    table = pd.DataFrame(rows)
    table.insert(0, "Compared field", table["Field"].map(lambda f: label(HISTORY_FIELDS, f)))
    return table.rename(columns={"Field": "Stored field"})[
        ["Compared field", "Result", "Detail", "Stored field"]]


def _summary(comparison):
    """One line counting the comparison classes, in the engine's own terms."""

    order = ("CONSISTENT", "DIFFERS", "SINGLE_OBSERVATION", "NOT_STATED", "NOT_CAPTURED")
    counts = {status: sum(r["status"] == status for r in comparison.values()) for status in order}
    words = {"CONSISTENT": "consistent", "DIFFERS": "differ between published records",
             "SINGLE_OBSERVATION": "stated in one record only", "NOT_STATED": "not stated",
             "NOT_CAPTURED": "not captured (legacy)"}
    parts = [f"{n} {words[s]}" for s, n in counts.items() if n]
    return f"{len(comparison)} fields compared: " + "; ".join(parts) + "."


def _audit(audit):
    if audit["observations_only_in_incomplete_batches"]:
        st.warning(f"{audit['observations_only_in_incomplete_batches']} observation(s) exist only in "
                   "incomplete import attempts and are not shown as published history.")
    if audit["quarantined_records"]:
        st.warning(f"{audit['quarantined_records']} quarantined source record(s) have identifiers "
                   "that cannot be trusted; they are not shown as evidence.")


def _load_relationship(connect_db, child, parent, kind):
    connection = connect_db()
    try:
        return (relationship_history(connection, child, parent, kind),
                entity_history(connection, child), entity_history(connection, parent),
                parent_history(connection, child))
    finally:
        connection.close()


def _load_entities(connect_db, leis):
    connection = connect_db()
    try:
        return {lei: entity_history(connection, lei) for lei in leis}
    finally:
        connection.close()


def render_relationship_history(case, connect_db):
    """The GLEIF history of the selected relationship and its two entities."""

    with st.expander("GLEIF source history (global, not case evidence)"):
        st.caption(GLOBAL_CAPTION)
        try:
            history, child, parent, parents = _load_relationship(
                connect_db, case["child_lei"], case["parent_lei"], case["relationship_type"])
        except Exception as error:  # noqa: BLE001 - shown, never hidden
            st.error(f"GLEIF history is unavailable: {error}")
            return

        if history["interpretation"]:
            st.info(history["interpretation"])
        st.markdown("**Relationship observations**")
        st.dataframe(_observations(relationship_rows(history)), hide_index=True, width="stretch")
        st.markdown("**Comparison of the published records**")
        if history["comparison"]:
            st.write(_summary(history["comparison"]))
        st.dataframe(_comparison(comparison_rows(history["comparison"])), hide_index=True,
                     width="stretch")
        determination = history["assessment"].get("corporate_change_determination")
        if determination:
            st.markdown(f"**Corporate-change reading:** {readable(determination)} "
                        f"(`{determination}`)")
        st.caption(history["assessment"]["note"])
        for absence in history["absences"]:
            st.write(f"Not in {absence['source']} (data valid as of {absence['data_valid_as_of_utc']}): "
                     f"{absence['meaning']}. {absence['note']}")
        _audit(history["audit"])

        others = [r for r in parents["relationships"]
                  if (r["parent_lei"], r["relationship_type"]) != (case["parent_lei"],
                                                                   case["relationship_type"])]
        if others:
            st.markdown("**Other parent relationships reported for this entity**")
            st.dataframe(pd.DataFrame([{
                "Parent LEI": r["parent_lei"], "Relationship": r["relationship_type"],
                "Reported in": len(r["reported_in"]), "Absent from": len(r["absent_from"])}
                for r in others]), hide_index=True, width="stretch")

        for label, entity in (("Reporting entity", child), ("Related entity", parent)):
            st.markdown(f"**{label}: entity observations**")
            if entity["entries"]:
                st.dataframe(_observations(entity_rows(entity)), hide_index=True, width="stretch")
                differing = entity["assessment"]["differing_fields"]
                if differing:
                    st.caption(f"Published records differ in: {', '.join(differing)}. "
                               + entity["assessment"]["note"])
                for event in entity["stated_events"]:
                    st.caption(f"{event['source']} states {event['type']} ({event['event_status']}), "
                               f"effective {event['effective_as_stated'] or 'not stated'}. "
                               f"{event['note']}")
            else:
                st.info("No published GLEIF entity observations are recorded for this LEI.")
            _audit(entity["audit"])


def render_observed_names(leis, connect_db):
    """Observed GLEIF names for the given LEIs; read-only help for mapping review."""

    leis = sorted(set(leis))
    if not leis:
        return
    with st.expander("Observed GLEIF names (read-only)"):
        st.caption(
            "Names that published GLEIF records state for the case parties and for the LEIs "
            "already named in mappings. They may help assess a mapping; they never create or "
            "change one, and a matching name is not proof of identity. " + GLOBAL_CAPTION)
        try:
            histories = _load_entities(connect_db, leis)
        except Exception as error:  # noqa: BLE001
            st.error(f"Observed names are unavailable: {error}")
            return
        rows = [row for lei in leis for row in name_rows(lei, histories[lei])]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        else:
            st.info("No published GLEIF entity observations are recorded for these LEIs.")
        for lei in leis:
            if histories[lei]["comparison"]["legal_name"]["status"] == "DIFFERS":
                st.warning(f"The published legal names of {lei} differ between records; this is "
                           "not a verified name change.")
            _audit(histories[lei]["audit"])
