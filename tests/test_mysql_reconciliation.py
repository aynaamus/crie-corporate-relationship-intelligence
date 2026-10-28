
import os
import uuid
from pathlib import Path

import mysql.connector
import pytest
from dotenv import load_dotenv

from src.claim_corrections import correct_claim
from src.claim_reconciliation import (
    build_reconciled_comparison,
    reconcile_claims,
)
from src.load_current_disclosures import load_current_disclosures


ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE = "corporate_intelligence_test"

ENTITY = "Fictional Harbour Holdings plc"


@pytest.mark.skipif(
    os.getenv("RUN_CRI_DB_TEST") != "1",
    reason="Requires explicit integration-test opt-in",
)
def test_real_mysql_reconciliation():
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
    source_ids = []
    claim_ids = {}

    try:
        # Verify database identity before any insert.
        cursor.execute("SELECT DATABASE() AS name")
        assert cursor.fetchone()["name"] == TEST_DATABASE

        cursor.execute(
            "INSERT INTO investigation_cases (case_name) VALUES (%s)",
            ("Synthetic reconciliation test",),
        )
        case_id = cursor.lastrowid

        for title in ("Fictional report A", "Fictional report B",
                      "Fictional report C"):
            cursor.execute(
                """
                INSERT INTO case_sources (case_id, document_title)
                VALUES (%s, %s)
                """,
                (case_id, title),
            )
            source_ids.append(cursor.lastrowid)

        a, b, c = source_ids

        def insert(label, source_id, metric, year, value):
            cursor.execute(
                """
                INSERT INTO disclosure_claims (
                    source_id, entity_name, metric,
                    report_year, period_end, value_text,
                    normalized_value, source_location,
                    claim_fingerprint
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    source_id, ENTITY, metric, year,
                    f"{year}-12-31", value,
                    value.removesuffix("%"),
                    f"Page {len(claim_ids) + 1}",
                    uuid.uuid4().hex * 2,
                ),
            )
            claim_ids[label] = str(cursor.lastrowid)

        # Corroborated: two sources agree in both years.
        insert("agree_24a", a, "harbour_interest_pct", 2024, "49")
        insert("agree_24b", b, "harbour_interest_pct", 2024, "49%")
        insert("agree_25a", a, "harbour_interest_pct", 2025, "49")
        insert("agree_25b", b, "harbour_interest_pct", 2025, "49")

        # Conflict in 2025: two sources agree, one disagrees.
        insert("conf_24", a, "parent_interest_pct", 2024, "51")
        insert("conf_25a", a, "parent_interest_pct", 2025, "51")
        insert("conf_25b", b, "parent_interest_pct", 2025, "51")
        insert("conf_25c", c, "parent_interest_pct", 2025, "60")

        connection.commit()

        # Strict loading still refuses competing claims.
        with pytest.raises(ValueError, match="Multiple current"):
            load_current_disclosures(
                case_id=case_id, connection=connection
            )

        claims = load_current_disclosures(
            case_id=case_id, strict=False, connection=connection
        )

        assert sorted(claims["claim_id"], key=int) == sorted(
            claim_ids.values(), key=int
        )

        reconciled = reconcile_claims(claims).set_index(
            ["metric", "report_year"]
        )

        agree = reconciled.loc[("harbour_interest_pct", "2025")]
        # Without provenance, agreeing sources are not corroboration.
        assert agree["reconciliation_status"] == "INDEPENDENCE_UNVERIFIED"
        assert not agree["corroborated"]
        assert agree["source_ids"] == f"{a};{b}"

        conflict = reconciled.loc[("parent_interest_pct", "2025")]
        assert conflict["reconciliation_status"] == "UNRESOLVED_CONFLICT"
        assert conflict["conflict_scope"] == "CROSS_SOURCE"
        assert conflict["reconciled_value"] == ""
        assert conflict["claim_ids"] == ";".join(
            claim_ids[k] for k in ("conf_25a", "conf_25b", "conf_25c")
        )

        comparison = build_reconciled_comparison(claims).set_index(
            "metric"
        )

        assert comparison.loc[
            "harbour_interest_pct", "comparison_status"
        ] == "UNCHANGED"
        assert comparison.loc[
            "parent_interest_pct", "comparison_status"
        ] == "UNRESOLVED_CONFLICT"

        # A correction supersedes the claim it replaces;
        # only the replacement takes part in reconciliation.
        connection.commit()
        result = correct_claim(
            connection, case_id, int(claim_ids["conf_25c"]),
            "51", "Page 8", "Fictional transcription correction",
        )
        replacement = str(result["replacement_claim_id"])

        claims = load_current_disclosures(
            case_id=case_id, strict=False, connection=connection
        )
        assert claim_ids["conf_25c"] not in set(claims["claim_id"])
        assert replacement in set(claims["claim_id"])

        row = build_reconciled_comparison(claims).set_index(
            "metric"
        ).loc["parent_interest_pct"]

        assert row["comparison_status"] == "UNCHANGED"
        assert row["reconciliation_2025"] == "INDEPENDENCE_UNVERIFIED"
        assert replacement in row["claim_ids_2025"].split(";")

        connection.commit()

    finally:
        # Clean up only the fictional records created here.
        connection.rollback()

        try:
            if source_ids:
                placeholders = ", ".join(["%s"] * len(source_ids))

                # Corrections are append-only; the test schema allows
                # deleting synthetic rows only under this flag.
                cursor.execute("SET @crie_claim_maintenance = 1")
                cursor.execute(
                    f"""
                    DELETE cc FROM claim_corrections cc
                    JOIN disclosure_claims c
                        ON c.claim_id = cc.original_claim_id
                    WHERE c.source_id IN ({placeholders})
                    """,
                    source_ids,
                )
                cursor.execute("SET @crie_claim_maintenance = 0")
                cursor.execute(
                    f"DELETE FROM disclosure_claims "
                    f"WHERE source_id IN ({placeholders})",
                    source_ids,
                )
                cursor.execute(
                    f"DELETE FROM case_sources "
                    f"WHERE source_id IN ({placeholders})",
                    source_ids,
                )

            if case_id is not None:
                cursor.execute(
                    "DELETE FROM investigation_cases WHERE case_id = %s",
                    (case_id,),
                )

            connection.commit()
        finally:
            cursor.close()
            connection.close()
