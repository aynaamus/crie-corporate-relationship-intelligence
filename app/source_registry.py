
from datetime import date
from urllib.parse import urlparse
from assessment_editor import render_assessment_editor
from workflow_ui import render_assessment_history
from source_editor import render_source_editor
from source_relationships_ui import render_source_relationships

import pandas as pd
import streamlit as st

import access

from display import utc


def render_source_registry(case_id, connect_db):

    st.divider()
    st.subheader("Independent Source Registry", anchor="crie-sources")

    st.caption(
        "Record independently located documents and "
        "your assessment of their evidentiary value."
    )

    with st.expander("Register a new source"):
        with st.form(
            f"source_form_{case_id}",
            clear_on_submit=True
        ):
            title = st.text_input(
                "Document title"
            )

            publisher = st.text_input(
                "Publisher or issuing organization"
            )

            url = st.text_input(
                "Source URL",
                placeholder="https://..."
            )

            publication_date = st.text_input(
                "Publication date (optional)",
                placeholder="YYYY-MM-DD"
            )

            assessment = st.selectbox(
                "Evidence assessment",
                [
                    "NOT_REVIEWED",
                    "SUPPORTS",
                    "CONTRADICTS",
                    "INCONCLUSIVE",
                ]
            )

            notes = st.text_area(
                "Analyst observations",
                placeholder=(
                    "What does the document establish? "
                    "What remains uncertain?"
                )
            )

            access.notice()
            submitted = st.form_submit_button(
                "Add source",
                **access.submit_options(),
            )

    if submitted:

        parsed = urlparse(url.strip())

        valid_url = (
            parsed.scheme in ("http", "https")
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and not any(c.isspace() for c in url)
        )

        if not title.strip():
            st.error("Document title is required.")

        elif not valid_url:
            st.error("Enter a valid HTTP(S) source URL.")

        elif assessment != "NOT_REVIEWED" and not notes.strip():
            st.error(
                "Explain your assessment in the analyst notes."
            )

        else:
            try:
                document_date = (
                    date.fromisoformat(publication_date.strip())
                    if publication_date.strip()
                    else None
                )

                db = connect_db()
                cursor = db.cursor()

                try:
                    access.require_write(db, case_id)
                    cursor.execute(
                        """
                        INSERT INTO case_sources (
                            case_id,
                            document_title,
                            publisher,
                            source_url,
                            publication_date,
                            evidence_assessment,
                            analyst_notes
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            case_id,
                            title.strip(),
                            publisher.strip() or None,
                            url.strip(),
                            document_date,
                            assessment,
                            notes.strip() or None,
                        )
                    )
                    new_source_id = cursor.lastrowid

                    cursor.execute(
                        """
                        INSERT INTO source_assessment_history (
                            source_id,
                            event_type,
                            previous_assessment,
                            new_assessment,
                            review_reason
                        )
                        VALUES (%s, 'CREATED', NULL, %s, %s)
                        """,
                        (
                            new_source_id,
                            assessment,
                            notes.strip() or
                            "Source initially registered.",
                        ),
                    )
                    db.commit()
                    st.success("Source saved successfully.")

                except Exception:
                    db.rollback()
                    raise

                finally:
                    cursor.close()
                    db.close()

            except access.AccessDenied as error:
                st.error(str(error))

            except ValueError:
                st.error(
                    "Publication date must use YYYY-MM-DD."
                )

    # Retrieve previously recorded sources
    db = connect_db()
    cursor = db.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                source_id,
                document_title,
                publisher,
                source_url,
                publication_date,
                evidence_assessment,
                analyst_notes,
                added_at
            FROM case_sources
            WHERE case_id = %s
            ORDER BY source_id DESC
            """,
            (case_id,)
        )

        sources = cursor.fetchall()

    finally:
        cursor.close()
        db.close()

    st.subheader("Registered sources")
    st.metric("Documents registered", len(sources))

    if not sources:
        st.info(
            "No independent documentary sources "
            "have been registered yet."
        )
        return

    df = pd.DataFrame(sources)

    st.dataframe(
        pd.DataFrame({
            "Source": [f"S{i}" for i in df["source_id"]],
            "Document": df["document_title"],
            "Publisher": df["publisher"].fillna("Not recorded"),
            "Published": df["publication_date"].map(
                lambda d: str(d) if pd.notna(d) else "Not recorded"),
            "Current assessment": df["evidence_assessment"],
            "Registered": df["added_at"].map(utc),
        }),
        hide_index=True,
        width="stretch",
    )

    for source in sources:
        with st.expander(source["document_title"]):
            st.write(
                "Publisher:",
                source["publisher"] or "Not recorded"
            )

            st.write(
                "Assessment:",
                source["evidence_assessment"]
            )

            st.write(
                source["analyst_notes"] or
                "No analyst observations recorded."
            )

            st.link_button(
                "Open original source",
                source["source_url"]
            )
    render_assessment_editor(
        case_id,
        sources,
        connect_db
    )
    render_assessment_history(case_id, connect_db)
    render_source_editor(
        case_id,
        sources,
        connect_db
    )
    render_source_relationships(
        case_id,
        connect_db
    )