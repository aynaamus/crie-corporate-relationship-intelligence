
from pathlib import Path
import os
import re

import pandas as pd
import mysql.connector
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)

df = pd.read_csv(
    ROOT / "corporate_relationships_named.csv",
    dtype=str,
    keep_default_na=False
)

# Validate before importing
required = [
    "child_lei", "parent_lei",
    "child_name", "parent_name",
    "relationship_type",
    "relationship_status", "registration_status"
]

if not set(required).issubset(df.columns):
    raise ValueError("Missing required CSV columns")

if not (
    df["relationship_status"].eq("ACTIVE").all()
    and df["registration_status"].eq("PUBLISHED").all()
):
    raise ValueError("Unexpected relationship status")

# Build a dictionary of legal entities
entities = {}

for row in df.itertuples(index=False):
    for lei, name in (
        (row.child_lei, row.child_name),
        (row.parent_lei, row.parent_name),
    ):
        if not re.fullmatch(r"[A-Z0-9]{20}", lei):
            raise ValueError(f"Invalid LEI: {lei}")

        if not name.strip():
            raise ValueError(f"Missing name for {lei}")

        entities[lei] = name

print("Unique entities:", len(entities))
print("Relationships:", len(df))

# Connect to MySQL
connection = mysql.connector.connect(
    host=os.getenv("MYSQL_HOST"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER"),
    password=os.getenv("MYSQL_PASSWORD"),
    database=os.getenv("MYSQL_DATABASE"),
)

cursor = connection.cursor()

try:
    # Insert companies not already in the database
    cursor.executemany(
        """
        INSERT IGNORE INTO legal_entities
        (lei, legal_name)
        VALUES (%s, %s)
        """,
        list(entities.items())
    )

    # Prepare the relationship records
    relationships = [
        (
            row.child_lei,
            row.parent_lei,
            row.relationship_type,
            row.relationship_status,
            row.registration_status,
        )
        for row in df.itertuples(index=False)
    ]

    # Insert relationships without duplicating records
    cursor.executemany(
        """
        INSERT IGNORE INTO corporate_relationships
        (
            child_lei,
            parent_lei,
            relationship_type,
            relationship_status,
            registration_status
        )
        VALUES (%s, %s, %s, %s, %s)
        """,
        relationships
    )

    cursor.execute(
        "SELECT COUNT(*) FROM corporate_relationships"
    )
    total = cursor.fetchone()[0]

    if total != len(df.drop_duplicates(
        ["child_lei", "parent_lei", "relationship_type"]
    )):
        raise ValueError(
            "Relationship count mismatch; check import data"
        )

    connection.commit()
    print("Relationships in MySQL:", total)
    print("Import completed")

except Exception:
    connection.rollback()
    raise

finally:
    cursor.close()
    connection.close()
