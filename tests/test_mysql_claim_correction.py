
import os
import uuid
from pathlib import Path

import mysql.connector
import pytest
from dotenv import load_dotenv

from src.claim_corrections import correct_claim


ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE = "corporate_intelligence_test"


@pytest.mark.skipif(
    os.getenv("RUN_CRI_DB_TEST") != "1",
    reason="Requires explicit integration-test opt-in",
)
def test_real_mysql_claim_correction():
    load_dotenv(ROOT / ".env")

    connection = mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=TEST_DATABASE,
    )

    cursor = connection.cursor(dictionary=True)

    case_id = None
    source_id = None
    original_id = None

    try:
        cursor.execute("SELECT DATABASE() AS name")
        assert cursor.fetchone()["name"] == TEST_DATABASE

        # 1. Create a fictional investigation.
        cursor.execute(
            """
            INSERT INTO investigation_cases (case_name)
            VALUES (%s)
            """,
            ("Synthetic integration test",),
        )
        case_id = cursor.lastrowid

        cursor.execute(
            """
            INSERT INTO case_sources (
                case_id, document_title
            )
            VALUES (%s, %s)
            """,
            (case_id, "Fictional annual report"),
        )
        source_id = cursor.lastrowid

        # 2. Insert a fictional original claim.
        cursor.execute(
            """
            INSERT INTO disclosure_claims (
                source_id,
                entity_name,
                metric,
                report_year,
                period_end,
                value_text,
                normalized_value,
                source_location,
                claim_fingerprint
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s
            )
            """,
            (
                source_id,
                "Example Corporation",
                "equity_interest_pct",
                2025,
                "2025-12-31",
                "49",
                "49",
                "Page 12",
                uuid.uuid4().hex * 2,
            ),
        )

        original_id = cursor.lastrowid
        connection.commit()

        # 3. Perform a real correction.
        result = correct_claim(
            connection=connection,
            case_id=case_id,
            original_claim_id=original_id,
            new_value="48",
            new_location="Page 12",
            reason="Verified fictional transcription error",
        )

        replacement_id = result["replacement_claim_id"]

        # 4. Original must remain; replacement must exist.
        cursor.execute(
            """
            SELECT claim_id, value_text
            FROM disclosure_claims
            WHERE source_id = %s
            ORDER BY claim_id
            """,
            (source_id,),
        )

        claims = cursor.fetchall()

        assert len(claims) == 2
        assert {r["value_text"] for r in claims} == {
            "49", "48"
        }

        # 5. Current view must contain only replacement.
        cursor.execute(
            """
            SELECT claim_id, value_text
            FROM v_current_disclosure_claims
            WHERE source_id = %s
            """,
            (source_id,),
        )

        current = cursor.fetchall()

        assert len(current) == 1
        assert current[0]["claim_id"] == replacement_id
        assert current[0]["value_text"] == "48"

        # 6. Correction history must be recorded.
        cursor.execute(
            """
            SELECT replacement_claim_id
            FROM claim_corrections
            WHERE original_claim_id = %s
            """,
            (original_id,),
        )

        assert (
            cursor.fetchone()["replacement_claim_id"]
            == replacement_id
        )

        # Finish the read transaction before starting another.
        connection.commit()

        # 7. An already-superseded claim must be rejected.
        with pytest.raises(ValueError, match="superseded"):
            correct_claim(
                connection,
                case_id,
                original_id,
                "47",
                "Page 12",
                "Another fictional correction",
            )

        # 8. Verify the failed attempt saved nothing.
        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM disclosure_claims
            WHERE source_id = %s
            """,
            (source_id,),
        )

        assert cursor.fetchone()["total"] == 2

    finally:
        # Clean up only the fictional records created here.
        connection.rollback()

        try:
            if original_id is not None:
                # Corrections are append-only; the test schema allows
                # deleting synthetic rows only under this flag.
                cursor.execute("SET @crie_claim_maintenance = 1")
                cursor.execute(
                    """
                    DELETE FROM claim_corrections
                    WHERE original_claim_id = %s
                    """,
                    (original_id,),
                )
                cursor.execute("SET @crie_claim_maintenance = 0")

            if source_id is not None:
                cursor.execute(
                    """
                    DELETE FROM disclosure_claims
                    WHERE source_id = %s
                    """,
                    (source_id,),
                )

                cursor.execute(
                    """
                    DELETE FROM case_sources
                    WHERE source_id = %s
                    """,
                    (source_id,),
                )

            if case_id is not None:
                cursor.execute(
                    """
                    DELETE FROM investigation_cases
                    WHERE case_id = %s
                    """,
                    (case_id,),
                )

            connection.commit()
        finally:
            cursor.close()
            connection.close()
