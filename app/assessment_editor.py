
import mysql.connector
import pandas as pd
import streamlit as st

import access

from display import utc


ASSESSMENTS = [
    "NOT_REVIEWED",
    "SUPPORTS",
    "CONTRADICTS",
    "INCONCLUSIVE",
]


def render_assessment_editor(case_id, sources, connect_db):
    if not sources:
        return

    st.divider()
    st.subheader("Evidence Assessment Editor")

    st.caption(
        "Review a document, record your reasoning, "
        "and preserve previous assessments."
    )

    source_map = {
        int(source["source_id"]): source
        for source in sources
    }

    selected_id = st.selectbox(
        "Select a document to review",
        options=list(source_map),
        format_func=lambda sid: (
            source_map[sid]["document_title"]
        ),
        key=f"source_review_selection_{case_id}",
    )

    selected = source_map[selected_id]
    current_assessment = selected["evidence_assessment"]

    st.write("**Current assessment:**", current_assessment)

    with st.form(
        f"assessment_form_{case_id}_{selected_id}"
    ):
        new_assessment = st.selectbox(
            "Revised assessment",
            options=ASSESSMENTS,
            index=ASSESSMENTS.index(current_assessment),
        )

        reason = st.text_area(
            "Evidence and reasoning",
            placeholder=(
                "Which pages or disclosures did you examine? "
                "What does the document establish? "
                "What remains uncertain?"
            ),
            height=180,
        )

        access.notice()
        submitted = st.form_submit_button(
            "Save evidence review",
            type="primary",
            **access.submit_options(),
        )

    if submitted:
        if not reason.strip():
            st.error(
                "A written explanation is required "
                "for every review."
            )
        else:
            db = connect_db()
            cursor = db.cursor(dictionary=True)
            saved = False

            try:
                access.require_write(db, case_id)
                db.start_transaction()

                # Lock the source during the update.
                cursor.execute(
                    """
                    SELECT evidence_assessment
                    FROM case_sources
                    WHERE source_id = %s
                      AND case_id = %s
                    FOR UPDATE
                    """,
                    (selected_id, case_id),
                )

                current = cursor.fetchone()

                if current is None:
                    raise ValueError(
                        "Source not found in this case."
                    )

                previous = current["evidence_assessment"]

                if previous != current_assessment:
                    raise ValueError(
                        "The source assessment changed "
                        "since the page loaded. Refresh "
                        "before saving."
                    )
                # Check whether this exact review was already saved.
                cursor.execute(
                    """
                    SELECT new_assessment, review_reason
                    FROM source_assessment_history
                    WHERE source_id = %s
                    AND event_type = 'REVIEW'
                    ORDER BY history_id DESC
                    LIMIT 1
                    """,
                    (selected_id,),
                    )
                last_review = cursor.fetchone()
                if (
                    last_review
                    and last_review["new_assessment"] == new_assessment
                    and (last_review["review_reason"] or "").strip()
                    == reason.strip()
                ):
                    raise ValueError(
                        "This exact review is already saved. "
                        "No duplicate entry was created."
                    )
                    
                # Update current assessment if changed.
                if new_assessment != previous:
                    cursor.execute(
                        """
                        UPDATE case_sources
                        SET evidence_assessment = %s
                        WHERE source_id = %s
                          AND case_id = %s
                        """,
                        (
                            new_assessment,
                            selected_id,
                            case_id,
                        ),
                    )

                # Always preserve the new review entry,
                # even if the assessment stayed the same.
                cursor.execute(
                    """
                    INSERT INTO source_assessment_history (
                        source_id,
                        event_type,
                        previous_assessment,
                        new_assessment,
                        review_reason
                    )
                    VALUES (%s, 'REVIEW', %s, %s, %s)
                    """,
                    (
                        selected_id,
                        previous,
                        new_assessment,
                        reason.strip(),
                    ),
                )

                # Both changes succeed or both roll back.
                db.commit()
                saved = True

            except (mysql.connector.Error, ValueError, access.AccessDenied) as error:
                db.rollback()
                st.error(str(error))

            finally:
                cursor.close()
                db.close()

            if saved:
                st.rerun()

    # Retrieve the saved assessment history.
    db = connect_db()
    cursor = db.cursor(dictionary=True)

    try: 
        cursor.execute(
            """
            SELECT
                h.history_id,
                h.event_type,
                h.previous_assessment,
                h.new_assessment,
                h.review_reason,
                h.recorded_at_utc,
                
                CASE
                    WHEN a.annotation_id IS NOT NULL
                    THEN 'ACCIDENTAL DUPLICATE'
                    ELSE 'RETAINED'
                END AS entry_status

            FROM source_assessment_history AS h

            JOIN case_sources AS s
                ON s.source_id = h.source_id

            LEFT JOIN source_history_annotations AS a
                ON a.history_id = h.history_id
                AND a.annotation_type = 'ACCIDENTAL_DUPLICATE'

            WHERE h.source_id = %s
                AND s.case_id = %s

            ORDER BY h.history_id DESC
            """,
            (selected_id, case_id),
        )

        history = cursor.fetchall()

    finally:
        cursor.close()
        db.close()

    st.subheader("Review history of the selected document")

    if not history:
        st.info("No assessment history recorded.")
        return

    
    history_df = pd.DataFrame(history)

    # Identify previously annotated duplicate reviews
    duplicate_mask = history_df["entry_status"].eq(
        "ACCIDENTAL DUPLICATE"
    )

    duplicate_count = int(duplicate_mask.sum())

    columns = {
        "history_id": "History ID",
        "event_type": "Event",
        "previous_assessment": "Previous",
        "new_assessment": "New assessment",
        "review_reason": "Reasoning",
        "recorded_at_utc": "Recorded",
        "entry_status": "Entry status",
    }
    history_df["recorded_at_utc"] = history_df["recorded_at_utc"].map(utc)

    # Only entries annotated as accidental duplicates are set apart; they
    # stay in the audit record below, unchanged.
    st.dataframe(
        history_df.loc[~duplicate_mask].drop(columns="entry_status").rename(columns=columns),
        hide_index=True,
        width="stretch",
    )

    if duplicate_count:
        with st.expander(
            f"Audit record: {duplicate_count} annotated accidental duplicate review(s)"
        ):
            st.caption(
                "Repeated submissions annotated as accidental duplicates. They are "
                "kept unchanged in the audit trail and are not counted as reviews."
            )
            st.dataframe(
                history_df.loc[duplicate_mask].rename(columns=columns),
                hide_index=True,
                width="stretch",
            )

