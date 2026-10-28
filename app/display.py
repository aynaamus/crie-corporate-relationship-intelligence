
"""
Investigator-facing labels for stored codes and timestamps. Display only:
stored values are never changed, and each label keeps the distinction its
code makes. An unknown code is shown as a readable form of itself, never
as something it does not say.
"""

import pandas as pd

RELATIONSHIP_TYPES = {
    "IS_DIRECTLY_CONSOLIDATED_BY": "Directly consolidated by",
    "IS_ULTIMATELY_CONSOLIDATED_BY": "Ultimately consolidated by",
    "IS_INTERNATIONAL_BRANCH_OF": "International branch of",
    "IS_SUBFUND_OF": "Sub-fund of",
}

# GLEIF's own validation classes. They describe how GLEIF validated a record;
# they are not CRIE's corroboration assessment.
GLEIF_VALIDATION = {
    "FULLY_CORROBORATED": "Fully corroborated by GLEIF",
    "PARTIALLY_CORROBORATED": "Partially corroborated by GLEIF",
    "ENTITY_SUPPLIED_ONLY": "Entity-supplied only",
}

VALIDATION_DOCUMENTS = {
    "ACCOUNTS_FILING": "Accounts filing",
    "REGULATORY_FILING": "Regulatory filing",
    "SUPPORTING_DOCUMENTS": "Supporting documents",
    "CONTRACTS": "Contracts",
    "OTHER_OFFICIAL_DOCUMENTS": "Other official documents",
}

PERIOD_TYPES = {
    "ACCOUNTING_PERIOD": "Accounting period",
    "RELATIONSHIP_PERIOD": "Relationship period",
    "DOCUMENT_FILING_PERIOD": "Document filing period",
}

PERIOD_SOURCES = {
    "GLEIF_OBSERVATION": "GLEIF observation",
    "LEGACY_PERIOD_TABLE": "Legacy import",
}

ORIGINS = {
    "GLEIF_OBSERVATION": "GLEIF observation",
    "LEGACY_IMPORT": "Legacy import",
}

# Field names compared by the GLEIF history engine.
HISTORY_FIELDS = {
    "relationship_status": "Relationship status",
    "registration_status": "Registration status",
    "validation_sources": "GLEIF validation class",
    "validation_documents": "Validation documents",
    "validation_reference": "Validation reference",
    "start_node_id_type": "Reporting-entity identifier type",
    "end_node_id_type": "Related-entity identifier type",
    "periods": "Reported periods",
    "qualifiers": "Qualifiers",
    "quantifiers": "Quantifiers",
    "managing_lou": "Managing LOU",
    "legal_name": "Legal name",
    "entity_status": "Entity status",
    "legal_jurisdiction": "Jurisdiction",
}


def readable(code):
    """A code such as NEEDS_MORE_EVIDENCE as "Needs more evidence"."""

    if code is None or (not isinstance(code, str) and pd.isna(code)):
        return "Not stated"
    text = str(code).replace("_", " ").strip().lower()
    return text[:1].upper() + text[1:]


def label(mapping, code):
    if code is None or (not isinstance(code, str) and pd.isna(code)):
        return "Not stated"
    return mapping.get(code, readable(code))


def relationship_type(code):
    return label(RELATIONSHIP_TYPES, code)


def gleif_validation(code):
    return label(GLEIF_VALIDATION, code)


def validation_documents(code):
    return label(VALIDATION_DOCUMENTS, code)


def period_type(code):
    return label(PERIOD_TYPES, code)


def period_source(code):
    return label(PERIOD_SOURCES, code)


def utc(value, missing="Not stated"):
    """A UTC timestamp as "2025-01-01 09:00 UTC" (seconds kept when present)."""

    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return missing
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("UTC").tz_localize(None)
    form = "%Y-%m-%d %H:%M:%S" if stamp.second or stamp.microsecond else "%Y-%m-%d %H:%M"
    return f"{stamp.strftime(form)} UTC"


def relationship_table(records, documents=True):
    """Relationship evidence rows with readable headers and values."""

    table = {
        "Reporting entity": records["child_name"],
        "Relationship": records["relationship_type"].map(relationship_type),
        "Consolidating parent": records["parent_name"],
        "GLEIF validation class": records["validation_sources"].map(gleif_validation),
    }
    if documents:
        table["Validation documents"] = records["validation_documents"].map(validation_documents)
    table["Data valid as of"] = records["snapshot_at_utc"].map(utc)
    return pd.DataFrame(table)


def period_table(periods, names=None):
    """Recorded periods with readable labels; an open end stays "Not stated"."""

    names = names or {}
    table = {}
    if "child_lei" in periods and names:
        table["Reporting entity"] = periods["child_lei"].map(lambda lei: names.get(lei, lei))
        table["Consolidating parent"] = periods["parent_lei"].map(lambda lei: names.get(lei, lei))
    table["Period type"] = periods["period_type"].map(period_type)
    table["Start (UTC)"] = periods["period_start_utc"].map(utc)
    table["End (UTC)"] = periods["period_end_utc"].map(utc)
    if "period_source" in periods:
        table["Recorded in"] = periods["period_source"].map(period_source)
    return pd.DataFrame(table)
