
"""
A short glossary of the terms the CRIE interface uses (Milestone 23B.3).
Each definition describes what CRIE actually records or computes, and
what it does not establish.
"""

import streamlit as st

GLOSSARY = {
    "Evidence and assessments": [
        ("Registered source", "A document recorded for one investigation, with its title, publisher, "
         "URL and dates as entered. Registering a source does not verify it."),
        ("Evidence assessment", "An investigator's recorded reading of a source: NOT_REVIEWED, "
         "SUPPORTS, CONTRADICTS or INCONCLUSIVE. Every revision is kept with its reasoning; an "
         "accidental duplicate review is annotated, never deleted."),
        ("Analyst case assessment", "The investigator's overall judgement of a case (NOT_ASSESSED, "
         "CORROBORATED, INCONCLUSIVE, CONTRADICTED). It is entered by hand and is not computed "
         "from the evidence."),
        ("Source relationship", "A recorded judgement of how two sources relate. Dependent kinds "
         "(SAME_DOCUMENT, REPUBLICATION, SHARED_PUBLISHER, SAME_CORPORATE_GROUP, "
         "QUOTES_OR_ATTRIBUTES, SAME_PRIMARY_DISCLOSURE) mean their agreement is not independent; "
         "INDEPENDENT_ORIGIN means independence was recorded; UNKNOWN means it was not established. "
         "A later assessment supersedes, but never erases, an earlier one."),
    ],
    "Claims and corrections": [
        ("Claim", "A value transcribed from a registered source, with its location, entity, metric "
         "and period. Stored claims are never edited."),
        ("Correction", "A new claim version replacing the active one, with a reason and, where "
         "given, supporting evidence. The earlier version stays recorded as superseded."),
        ("Restoration", "A new claim version that brings back an earlier version's value. It is "
         "itself a recorded correction; nothing is undone."),
    ],
    "Reconciliation and comparison": [
        ("SINGLE_CLAIM", "Only one claim states this value. Not corroboration."),
        ("REPEATED_SOURCE_CLAIMS", "Agreeing claims come from the same source."),
        ("DUPLICATE_DOCUMENT_CLAIMS", "Agreeing claims come from the same document registered "
         "more than once."),
        ("CONSISTENT_RELATED_SOURCES", "Sources agree but are recorded or known to be related "
         "(for example the same publisher). Not corroboration."),
        ("INDEPENDENCE_UNVERIFIED", "Sources agree, but their independence has not been "
         "established. Not corroboration."),
        ("CONFLICTING_INDEPENDENCE_ASSESSMENTS", "Recorded assessments disagree about whether "
         "the sources are independent, so agreement is not corroboration."),
        ("CONSISTENT_SOURCES", "Agreement between sources whose independent origin is recorded. "
         "This is the only status CRIE counts as corroborated."),
        ("UNRESOLVED_CONFLICT", "Claims for the same disclosure disagree. A review flag, not a "
         "finding of misrepresentation."),
        ("Corroboration", "In CRIE, only agreement between sources with recorded independent "
         "origin (CONSISTENT_SOURCES). Repeated reports, related sources and GLEIF records never "
         "corroborate a claim."),
        ("UNCHANGED / CHANGED", "Whether a metric's value differs between the compared years."),
        ("PERIOD_MISMATCH", "The compared claims describe reporting periods that do not "
         "correspond, so their values are not compared as like for like."),
    ],
    "Identity mapping": [
        ("Claim-subject identity", "An investigator's record of which legal entity the exact "
         "subject text of a case's claims refers to: MAPPED (one LEI, with evidence), AMBIGUOUS "
         "(several candidate LEIs), NO_LEI (the entity has none) or UNRESOLVED. Nothing is mapped "
         "automatically, and a mapping says nothing about ownership or control."),
        ("Observed GLEIF names", "Names that published GLEIF records state for an LEI. A matching "
         "name may help an investigator but is not proof of identity."),
    ],
    "GLEIF records": [
        ("GLEIF validation classification", "GLEIF's own label for how a record was validated "
         "(for example FULLY_CORROBORATED or ENTITY_SUPPLIED_ONLY). It is not a CRIE "
         "corroboration finding."),
        ("GLEIF observation", "What one GLEIF file stated about an entity or relationship, kept "
         "exactly as stated, with the file's SHA-256 and the import batch that recorded it."),
        ("Data valid as of", "A GLEIF file's ContentDate: the time its data is valid as of. It "
         "is not a publication date and not the date of any corporate event."),
        ("Accounting consolidation", "IS_DIRECTLY_CONSOLIDATED_BY / IS_ULTIMATELY_CONSOLIDATED_BY: "
         "the entity is reported as consolidated in the parent's financial statements. Not "
         "evidence of direct ownership or managerial control."),
        ("Legacy import / NOT CAPTURED", "Records imported before observation history existed. "
         "What they never recorded is NOT CAPTURED (unknown), never filled in."),
        ("Coverage", "What an import covered: a complete published dataset (every record of a "
         "FULL file, none quarantined), a delta file, a sample of a file, or a full scan with "
         "quarantined records. A sample is never a complete dataset."),
        ("Quarantine", "A validly read GLEIF record whose subject identifier fails LEI check "
         "digits or is provisional. It is kept as evidence of what the file said but never "
         "becomes a trusted entity, relationship or mapping."),
        ("Incomplete import", "An import attempt that has not completed (STARTED or FAILED). Its "
         "records are never shown as published history."),
    ],
    "Historical comparison": [
        ("CONSISTENT", "Every published record that states the field states the same value."),
        ("DIFFERS", "The published records differ. This does not establish that, or when, a "
         "corporate change occurred."),
        ("SINGLE_OBSERVATION", "Only one published record states the field."),
        ("NOT_STATED", "No published record states the field."),
        ("NOT_CAPTURED", "Only legacy records exist for the field, and they never recorded it."),
        ("INSUFFICIENT_EVIDENCE", "CRIE's reading of every GLEIF history: published records alone "
         "cannot show whether a corporate change happened."),
        ("Absence", "A relationship missing from a file is described by what that absence can "
         "mean (not in a full dataset, absent from a sample or delta, scope unknown, or "
         "undetermined because of quarantine). Absence is never evidence that it ended."),
    ],
    "Reports": [
        ("INTACT", "A stored report and its manifest match their recorded hashes."),
        ("CURRENT / STALE / UNKNOWN", "Whether present evidence matches the evidence a report "
         "was built from. STALE means only that the evidence differs; UNKNOWN means it could not "
         "be compared."),
        ("Snapshot version", "The evidence layout a report was built from (1 to 4). Version 4 "
         "adds case identity mappings and global GLEIF context, kept apart from the case's "
         "evidence. Earlier reports are never rewritten."),
    ],
}


def render_glossary():
    """The glossary, grouped as in the interface; read-only."""

    with st.expander("Glossary"):
        render_terms()


def render_terms():
    """Every glossary term, grouped."""

    st.caption("What CRIE's terms mean here, and what they do not establish.")
    for group, terms in GLOSSARY.items():
        st.markdown(f"**{group}**")
        for term, definition in terms:
            st.markdown(f"- **{term}**: {definition}")
