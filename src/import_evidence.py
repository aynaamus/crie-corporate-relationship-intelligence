
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import mysql.connector
import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)

df = pd.read_csv(
    ROOT / "reports/relationship_evidence.csv",
    dtype=str,
    keep_default_na=False,
)

key_columns = [
    "child_lei",
    "parent_lei",
    "relationship_type",
]

if df.empty or df.duplicated(key_columns).any():
    raise ValueError("Empty dataset or duplicate relationship keys")

if df["file_content_date"].nunique() != 1:
    raise ValueError("Inconsistent source snapshots")


def utc_datetime(value):
    if not value:
        return None
    dt = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )
    if dt.tzinfo is None:
        raise ValueError(f"Missing timezone: {value}")
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


snapshot = utc_datetime(df["file_content_date"].iloc[0])

rows = []

for row in df.itertuples(index=False):
    periods = json.loads(row.periods_json)

    if not isinstance(periods, list):
        raise ValueError("Invalid relationship periods")

    if not any(
        p.get("type") == "RELATIONSHIP_PERIOD"
        for p in periods
    ):
        raise ValueError(
            f"Missing relationship period: {row.child_lei}"
        )

    rows.append((
        row.child_lei,
        row.parent_lei,
        row.relationship_type,
        snapshot,
        utc_datetime(row.initial_registration_date),
        utc_datetime(row.last_update_date),
        utc_datetime(row.next_renewal_date),
        row.managing_lou or None,
        row.validation_sources,
        row.validation_documents,
        row.validation_reference or None,
        row.periods_json,
        row.source_file,
    ))

connection = mysql.connector.connect(
    host=os.getenv("MYSQL_HOST"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER"),
    password=os.getenv("MYSQL_PASSWORD"),
    database=os.getenv("MYSQL_DATABASE"),
)

cursor = connection.cursor()

try:
    cursor.execute("""
        SELECT child_lei, parent_lei, relationship_type
        FROM corporate_relationships
    """)
    known = set(cursor.fetchall())

    incoming = {
        (r[0], r[1], r[2]) for r in rows
    }

    if incoming != known:
        raise ValueError(
            "Evidence and relationship database keys differ"
        )

    cursor.execute("""
        SELECT child_lei, parent_lei, relationship_type
        FROM relationship_evidence
        WHERE snapshot_at_utc = %s
    """, (snapshot,))

    existing = set(cursor.fetchall())

    new_rows = [
        r for r in rows
        if (r[0], r[1], r[2]) not in existing
    ]

    if new_rows:
        cursor.executemany("""
            INSERT INTO relationship_evidence (
                child_lei,
                parent_lei,
                relationship_type,
                snapshot_at_utc,
                initial_registration_at_utc,
                last_update_at_utc,
                next_renewal_at_utc,
                managing_lou,
                validation_sources,
                validation_documents,
                validation_reference,
                periods_json,
                source_file
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s
            )
        """, new_rows)

    connection.commit()

    print("Source snapshot:", snapshot)
    print("Evidence records processed:", len(rows))
    print("New records inserted:", len(new_rows))
    print("Existing records skipped:", len(rows) - len(new_rows))

except Exception:
    connection.rollback()
    raise

finally:
    cursor.close()
    connection.close()
