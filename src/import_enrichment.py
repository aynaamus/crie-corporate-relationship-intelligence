
import os
from pathlib import Path

import pandas as pd
import mysql.connector
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)

source = ROOT / "reports/all_entities_enriched.csv"

df = pd.read_csv(
    source,
    dtype=str,
    keep_default_na=False
)

required = {"lei", "jurisdiction", "registered_country"}

if not required.issubset(df.columns):
    raise ValueError("Missing required columns")

if df["lei"].duplicated().any():
    raise ValueError("Duplicate LEIs in enrichment file")

if not df["lei"].str.fullmatch(r"[A-Z0-9]{20}").all():
    raise ValueError("Invalid LEI format")

connection = mysql.connector.connect(
    host=os.getenv("MYSQL_HOST"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER"),
    password=os.getenv("MYSQL_PASSWORD"),
    database=os.getenv("MYSQL_DATABASE"),
)

cursor = connection.cursor()

try:
    for row in df.itertuples(index=False):
        cursor.execute(
            "SELECT lei FROM legal_entities WHERE lei = %s",
            (row.lei,)
        )

        if cursor.fetchone() is None:
            raise ValueError(
                f"Entity missing from database: {row.lei}"
            )

        # Fill missing information without overwriting
        # existing values from the original GLEIF snapshot.
        cursor.execute(
            """
            UPDATE legal_entities
            SET
                jurisdiction = COALESCE(
                    NULLIF(jurisdiction, ''), %s
                ),
                country = COALESCE(
                    NULLIF(country, ''), %s
                )
            WHERE lei = %s
            """,
            (
                row.jurisdiction or None,
                row.registered_country or None,
                row.lei,
            )
        )

    connection.commit()
    print(f"Processed {len(df)} entity records")
    print("Database enrichment completed")

except Exception:
    connection.rollback()
    raise

finally:
    cursor.close()
    connection.close()
