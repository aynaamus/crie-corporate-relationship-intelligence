
import os
from pathlib import Path

import mysql.connector
import pandas as pd
from dotenv import load_dotenv

from src.disclosure_conflicts import audit_disclosure_claims


ROOT = Path(__file__).resolve().parents[1]

load_dotenv(ROOT / ".env")

CASE_ID = 1

OUTPUT = ROOT / "reports/database_conflict_audit.csv"


def main():
    connection = mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv(
            "MYSQL_DATABASE",
            "corporate_intelligence",
        ),
    )

    try:
        cursor = connection.cursor(dictionary=True)

        try:
            cursor.execute(
                """
                SELECT
                    c.claim_id,
                    c.source_id,
                    c.entity_name AS entity,
                    c.metric,
                    c.report_year,
                    c.period_end,
                    c.value_text AS value,
                    c.source_location
                FROM v_current_disclosure_claims c
                JOIN case_sources s
                    ON c.source_id = s.source_id
                WHERE s.case_id = %s
                ORDER BY c.claim_id
                """,
                (CASE_ID,),
            )

            records = cursor.fetchall()

        finally:
            cursor.close()

    finally:
        connection.close()

    if not records:
        raise ValueError(
            "No disclosure claims found for this case"
        )

    # Prepare MySQL records for the existing audit engine.
    facts = pd.DataFrame(records)

    facts["report_year"] = (
        facts["report_year"].astype(str)
    )

    facts["period_end"] = (
        facts["period_end"].astype(str)
    )

    facts["source_id"] = (
        facts["source_id"].astype(str)
    )

    # Reuse the conflict detection logic we tested.
    audit = audit_disclosure_claims(facts)

    OUTPUT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    audit.to_csv(OUTPUT, index=False)

    print("\nMYSQL DISCLOSURE CONFLICT AUDIT")
    print("Investigation case:", CASE_ID)
    print("Claims analyzed:", len(facts))

    print("\nRESULTS")
    print(
        audit[
            [
                "entity",
                "metric",
                "report_year",
                "source_count",
                "conflict_status",
            ]
        ].to_string(index=False)
    )

    print("\nSUMMARY")
    print(
        audit["conflict_status"].value_counts()
    )

    print("\nSaved to:", OUTPUT)


if __name__ == "__main__":
    main()
