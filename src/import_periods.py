
import os
import json
from pathlib import Path
from datetime import datetime, timezone

import mysql.connector
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)


def parse_date(value):
    if not value:
        return None

    dt = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )

    if dt.tzinfo is None:
        raise ValueError(
            f"Date is missing timezone: {value}"
        )

    return dt.astimezone(
        timezone.utc
    ).replace(tzinfo=None)


connection = mysql.connector.connect(
    host=os.getenv("MYSQL_HOST"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER"),
    password=os.getenv("MYSQL_PASSWORD"),
    database=os.getenv("MYSQL_DATABASE"),
)

cursor = connection.cursor(dictionary=True)

try:
    cursor.execute("""
        SELECT
            child_lei,
            parent_lei,
            relationship_type,
            snapshot_at_utc,
            periods_json
        FROM relationship_evidence
    """)

    evidence_records = cursor.fetchall()
    rows = []

    allowed_types = {
        "RELATIONSHIP_PERIOD",
        "ACCOUNTING_PERIOD",
        "DOCUMENT_FILING_PERIOD",
    }

    for record in evidence_records:
        periods = json.loads(record["periods_json"])

        for index, period in enumerate(periods):
            period_type = period.get("type")

            if period_type not in allowed_types:
                raise ValueError(
                    f"Unexpected period type: {period_type}"
                )

            start = parse_date(period.get("start"))
            end = parse_date(period.get("end"))

            if start and end and start > end:
                raise ValueError(
                    f"Invalid date range: {record['child_lei']}"
                )

            rows.append((
                record["child_lei"],
                record["parent_lei"],
                record["relationship_type"],
                record["snapshot_at_utc"],
                index,
                period_type,
                start,
                end,
            ))

    print("Evidence records:", len(evidence_records))
    print("Relationship periods found:", len(rows))

    insert_cursor = connection.cursor()

    try:
        insert_cursor.executemany("""
            INSERT INTO relationship_periods (
                child_lei,
                parent_lei,
                relationship_type,
                snapshot_at_utc,
                period_index,
                period_type,
                period_start_utc,
                period_end_utc
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                period_type = VALUES(period_type),
                period_start_utc = VALUES(period_start_utc),
                period_end_utc = VALUES(period_end_utc)
        """, rows)

        connection.commit()

    finally:
        insert_cursor.close()

    print("Relationship periods imported successfully")

except Exception:
    connection.rollback()
    raise

finally:
    cursor.close()
    connection.close()
