
import json
from datetime import date
from urllib.parse import urlsplit

import streamlit as st

import access


def render_source_editor(case_id, sources, connect_db):
    if not sources:
        return

    with st.expander("Edit registered source details (metadata corrections)"):

        source_map = {
            int(s["source_id"]): s for s in sources
        }

        source_id = st.selectbox(
            "Select source to edit",
            list(source_map),
            format_func=lambda sid: (
                f"#{sid} — {source_map[sid]['document_title']}"
            ),
            key=f"metadata_selection_{case_id}",
        )

        source = source_map[source_id]

        existing_date = source["publication_date"]
        date_text = (
            existing_date.isoformat()
            if existing_date else ""
        )

        with st.form(f"metadata_form_{case_id}_{source_id}"):
            title = st.text_input(
                "Document title",
                value=source["document_title"] or "",
            )

            publisher = st.text_input(
                "Publisher",
                value=source["publisher"] or "",
            )

            url = st.text_input(
                "Document URL",
                value=source["source_url"] or "",
            )

            publication_date = st.text_input(
                "Publication date (YYYY-MM-DD)",
                value=date_text,
            )

            reason = st.text_area(
                "Reason for correction",
                placeholder="Explain what was incorrect and why.",
            )

            access.notice()
            submitted = st.form_submit_button(
                "Save source corrections",
                type="primary",
                **access.submit_options(),
            )

        if not submitted:
            return

        parsed = urlsplit(url.strip())

        if not title.strip():
            st.error("Document title is required.")
            return

        if (
            len(title.strip()) > 500
            or len(publisher.strip()) > 255
            or len(url.strip()) > 2048
        ):
            st.error("A field exceeds its database length limit.")
            return

        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or any(c.isspace() for c in url)
        ):
            st.error("Enter a valid HTTP(S) URL.")
            return

        if not reason.strip():
            st.error("Explain why the source is being corrected.")
            return

        try:
            new_date = (
                date.fromisoformat(publication_date.strip())
                if publication_date.strip()
                else None
            )
        except ValueError:
            st.error("Use YYYY-MM-DD for the publication date.")
            return

        new_values = {
            "document_title": title.strip(),
            "publisher": publisher.strip() or None,
            "source_url": url.strip(),
            "publication_date": (
                new_date.isoformat() if new_date else None
            ),
        }

        # Values displayed when the form was opened.
        original_values = {
            "document_title": source["document_title"],
            "publisher": source["publisher"],
            "source_url": source["source_url"],
            "publication_date": date_text or None,
        }

        db = connect_db()
        cursor = db.cursor(dictionary=True)

        try:
            db.start_transaction()
            access.require_write(db, case_id)

            cursor.execute(
                """
                SELECT
                    document_title,
                    publisher,
                    source_url,
                    publication_date
                FROM case_sources
                WHERE source_id = %s
                  AND case_id = %s
                FOR UPDATE
                """,
                (source_id, case_id),
            )

            current = cursor.fetchone()

            if current is None:
                raise ValueError("Source was not found.")

            old_values = {
                "document_title": current["document_title"],
                "publisher": current["publisher"],
                "source_url": current["source_url"],
                "publication_date": (
                    current["publication_date"].isoformat()
                    if current["publication_date"]
                    else None
                ),
            }

            if old_values != original_values:
                raise ValueError(
                    "The source changed since you opened "
                    "this form. Refresh before editing."
                )

            if new_values == old_values:
                db.rollback()
                st.info("No changes were made.")
                return

            cursor.execute(
                """
                UPDATE case_sources
                SET
                    document_title = %s,
                    publisher = %s,
                    source_url = %s,
                    publication_date = %s
                WHERE source_id = %s
                  AND case_id = %s
                """,
                (
                    new_values["document_title"],
                    new_values["publisher"],
                    new_values["source_url"],
                    new_date,
                    source_id,
                    case_id,
                ),
            )

            cursor.execute(
                """
                INSERT INTO source_metadata_history (
                    source_id,
                    previous_metadata,
                    updated_metadata,
                    change_reason
                )
                VALUES (%s, %s, %s, %s)
                """,
                (
                    source_id,
                    json.dumps(old_values),
                    json.dumps(new_values),
                    reason.strip(),
                ),
            )

            db.commit()
            st.success("Source details updated.")
            st.rerun()

        except Exception as error:
            db.rollback()
            st.error(f"Correction failed: {error}")

        finally:
            cursor.close()
            db.close()
