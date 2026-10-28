
import os
from pathlib import Path
from urllib.parse import urlparse

import mysql.connector
import pandas as pd
import streamlit as st

import access
from dotenv import load_dotenv
from display import readable, utc

ROOT = Path(__file__).resolve().parents[1]
# Locally .env wins; a hosted installation uses only the platform's variables.
load_dotenv(ROOT / ".env", override=os.getenv("CRIE_HOSTED") != "1")


def connect_db():
    access.require_hosted_target()
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE"),
        time_zone="+00:00",
    )


def owner_condition(connection, user=None):
    """
    SQL (and parameters) restricting cases to those the user owns: nothing
    without the ownership schema; no case at all for a signed-out visitor.
    """

    if not access.ownership_installed(connection):
        return "", ()
    user = user or access.current_user()
    if user is None:
        return " AND FALSE", ()
    if user.subject.startswith("local:"):
        return " AND owner_subject IN (%s, '')", (user.subject,)
    return " AND owner_subject = %s", (user.subject,)


def case_key(case):
    snapshot = pd.Timestamp(
        case["snapshot_at_utc"]
    ).to_pydatetime()

    return (
        case["child_lei"],
        case["parent_lei"],
        case["relationship_type"],
        snapshot,
    )


def get_case(case):
    """The case by its permanent ID when the row carries one, else by its anchor."""

    db = connect_db()
    cursor = db.cursor(dictionary=True)

    try:
        case_id = case.get("case_id") if hasattr(case, "get") else None
        if case_id is not None and not pd.isna(case_id):
            cursor.execute("""
                SELECT case_id, review_status, assessment
                FROM investigation_cases
                WHERE case_id = %s
            """, (int(case_id),))
        else:
            condition, params = owner_condition(db)
            cursor.execute("""
                SELECT case_id, review_status, assessment
                FROM investigation_cases
                WHERE child_lei = %s
                  AND parent_lei = %s
                  AND relationship_type = %s
                  AND snapshot_at_utc = %s
            """ + condition, (*case_key(case), *params))

        return cursor.fetchone()

    finally:
        cursor.close()
        db.close()


def get_relationship_cases(case):
    """The user's cases on this relationship, whichever GLEIF observation anchors them."""

    db = connect_db()
    cursor = db.cursor()

    try:
        condition, params = owner_condition(db)
        cursor.execute("""
            SELECT case_id
            FROM investigation_cases
            WHERE child_lei = %s
              AND parent_lei = %s
              AND relationship_type = %s
        """ + condition + " ORDER BY case_id", (*case_key(case)[:3], *params))

        return [row[0] for row in cursor.fetchall()]

    finally:
        cursor.close()
        db.close()


def get_notes(case_id):
    db = connect_db()
    cursor = db.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT note_id, note_text,
                   source_url, created_at
            FROM case_notes
            WHERE case_id = %s
            ORDER BY note_id DESC
        """, (case_id,))

        return cursor.fetchall()

    finally:
        cursor.close()
        db.close()


def save_case(case, status, assessment, note, url):
    db = connect_db()
    cursor = db.cursor()

    try:
        existing = case.get("case_id") if hasattr(case, "get") else None
        owned = access.ownership_installed(db)

        if existing is not None and not pd.isna(existing) and owned:
            # An existing case: only its owner may change it.
            case_id = int(existing)
            access.require_write(db, case_id)
            cursor.execute("""
                UPDATE investigation_cases
                SET review_status = %s, assessment = %s
                WHERE case_id = %s
            """, (status, assessment, case_id))

        elif owned:
            # A new case belongs to the user who opens it.
            user = access.current_user()
            if user is None:
                raise access.AccessDenied("Sign in to open an investigation.")
            if access.is_guest(user):
                cursor.execute("SELECT COUNT(*) FROM investigation_cases "
                               "WHERE owner_subject = %s", (user.subject,))
                if cursor.fetchone()[0] >= access.GUEST_CASE_LIMIT:
                    raise access.AccessDenied(
                        f"A sandbox can hold {access.GUEST_CASE_LIMIT} investigations; "
                        "continue one of yours.")
            cursor.execute("""
                INSERT INTO investigation_cases (
                    child_lei, parent_lei,
                    relationship_type, snapshot_at_utc,
                    review_status, assessment, owner_subject
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (*case_key(case), status, assessment, user.subject))
            case_id = cursor.lastrowid

        else:
            cursor.execute("""
                INSERT INTO investigation_cases (
                    child_lei, parent_lei,
                    relationship_type, snapshot_at_utc,
                    review_status, assessment
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    review_status = VALUES(review_status),
                    assessment = VALUES(assessment)
            """, (*case_key(case), status, assessment))

            cursor.execute("""
                SELECT case_id
                FROM investigation_cases
                WHERE child_lei = %s
                  AND parent_lei = %s
                  AND relationship_type = %s
                  AND snapshot_at_utc = %s
            """, case_key(case))

            case_id = cursor.fetchone()[0]

        if note.strip():
            cursor.execute("""
                INSERT INTO case_notes (
                    case_id, note_text, source_url
                )
                VALUES (%s, %s, %s)
            """, (
                case_id,
                note.strip(),
                url.strip() or None,
            ))

        db.commit()
        return case_id

    except Exception:
        db.rollback()
        raise

    finally:
        cursor.close()
        db.close()


def batch_anchor_id(case):
    """The import batch behind this evidence row, or None (legacy import)."""

    if not hasattr(case, "get") or case.get("anchor_origin") != "GLEIF_BATCH":
        return None
    return int(case.get("anchor_batch_id"))


def render_case_management(case):
    st.divider()
    st.subheader("Investigative Case Management", anchor="crie-case")

    existing = get_case(case)

    # A relationship already under investigation is never opened twice: the
    # earlier case stays anchored on the observation it was opened on.
    if not existing:
        others = get_relationship_cases(case)
        if others:
            st.info(
                "This relationship is already investigated in "
                + ", ".join(f"Case #{case_id}" for case_id in others)
                + ". Open that case on the Investigations page; a new investigation "
                "is not created."
            )
            return

    # The investigation summary is filled in after any save below, so it
    # always shows the stored state (including a case opened just now).
    summary = st.container()

    st.caption(
        "Record your independent research separately "
        "from GLEIF's validation classification."
    )

    statuses = [
        "OPEN",
        "IN_PROGRESS",
        "NEEDS_MORE_EVIDENCE",
        "CLOSED",
    ]

    assessments = [
        "NOT_ASSESSED",
        "CORROBORATED",
        "INCONCLUSIVE",
        "CONTRADICTED",
    ]

    current_status = (
        existing["review_status"] if existing else "OPEN"
    )

    current_assessment = (
        existing["assessment"]
        if existing else "NOT_ASSESSED"
    )

    identity = "-".join(str(x) for x in case_key(case))

    form_area = (
        st.expander("Update the investigation status or add a research note")
        if existing else st.container(border=True)
    )
    if not existing:
        form_area.markdown("**Open an investigation on this relationship**")

    with form_area:
        with st.form(
            f"case_form_{identity}",
            clear_on_submit=True
        ):
            status = st.selectbox(
                "Investigation status",
                statuses,
                index=statuses.index(current_status),
            )

            assessment = st.selectbox(
                "Analyst case assessment (manual)",
                assessments,
                index=assessments.index(current_assessment),
                help=(
                    "Your overall judgement for this case. It is entered by "
                    "hand, is not computed from the recorded evidence, and is "
                    "not an automatic or independent corroboration finding. "
                    "Earlier values are not yet kept as history."
                ),
            )

            note = st.text_area(
                "Investigator notes",
                placeholder=(
                    "Describe the evidence examined, "
                    "your observations, and unresolved questions."
                ),
                height=160,
            )

            url = st.text_input(
                "Supporting document URL (optional)",
                placeholder="https://...",
            )

            # A new investigation anchored on an imported batch must be opened
            # knowingly: its wider GLEIF history has not been reviewed.
            batch_id = None if existing else batch_anchor_id(case)
            acknowledged = True
            if batch_id is not None:
                acknowledged = st.checkbox(
                    f"I understand this evidence row comes from GLEIF import batch "
                    f"#{batch_id}, that other GLEIF files may report this relationship "
                    "differently, and that this history has not yet been reviewed."
                )

            access.notice()
            submitted = st.form_submit_button(
                "Save investigation",
                type="primary",
                **access.submit_options(),
            )

    if submitted:
        parsed = urlparse(url.strip())

        if not acknowledged:
            st.error(
                "Confirm that you understand the limits of this imported "
                "evidence before opening an investigation on it."
            )

        elif url and (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
        ):
            st.error("Enter a valid HTTP(S) source URL.")

        elif url.strip() and not note.strip():
            st.error(
                "Add a note explaining the source reference."
            )

        elif (
            assessment != "NOT_ASSESSED"
            and assessment != current_assessment
            and not note.strip()
        ):
            st.error(
                "Document your reasoning when changing "
                "the analyst case assessment."
            )

        else:
            try:
                case_id = save_case(
                    case, status, assessment, note, url
                )
                st.success(
                    f"Case #{case_id} saved successfully."
                )
                existing = get_case(case)

            except access.AccessDenied as error:
                st.error(str(error))

            except mysql.connector.Error as error:
                st.error(f"Database error: {error}")

    if existing:
        with summary:
            with st.container(border=True):
                st.caption("Selected investigation")
                col1, col2, col3 = st.columns(3)
                col1.metric("Investigation", f"Case #{existing['case_id']}")
                col2.metric("Status", readable(existing["review_status"]))
                col3.metric(
                    "Analyst case assessment (manual)",
                    readable(existing["assessment"]),
                    help="Entered by hand; not computed from the recorded evidence.",
                )

        with summary:
            st.markdown("**Research notes** (findings and open questions, newest first)")

            notes = get_notes(existing["case_id"])

            if not notes:
                st.info("No research notes recorded yet.")

            for entry in notes:
                with st.container(border=True):
                    st.caption(
                        f"Note #{entry['note_id']} | "
                        f"{utc(entry['created_at'])}"
                    )

                    st.write(entry["note_text"])
