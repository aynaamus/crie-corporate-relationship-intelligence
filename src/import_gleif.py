
from pathlib import Path
import os

import pandas as pd
import mysql.connector
from dotenv import load_dotenv

# Locate project files

PROJECT_ROOT = Path(__file__).resolve().parents[1]

env_path = PROJECT_ROOT / ".env"

if not env_path.is_file():
    raise FileNotFoundError(
        f"Missing database configuration: {env_path}"
    )

load_dotenv(env_path)

required_vars = [
    "MYSQL_HOST",
    "MYSQL_USER",
    "MYSQL_PASSWORD",
    "MYSQL_DATABASE",
]

missing = [
    key for key in required_vars
    if not os.getenv(key)
]

if missing:
    raise RuntimeError(
        f"Missing MySQL settings: {', '.join(missing)}"
    )


csv_file = PROJECT_ROOT / "gleif_sample_1000.csv"

# Load records
df = pd.read_csv(
    csv_file,
    dtype=str,
    keep_default_na=False
)

# Clean and validate LEIs
df["lei"] = df["lei"].str.strip().str.upper()

valid_lei = df["lei"].str.fullmatch(r"[A-Z0-9]{20}")

print("Original records:", len(df))
print("Invalid LEI formats:", (~valid_lei).sum())

df = df.loc[valid_lei].copy()
df = df.drop_duplicates(subset=["lei"])

# Connect to MySQL
connection = mysql.connector.connect(
    host=os.getenv("MYSQL_HOST"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER"),
    password=os.getenv("MYSQL_PASSWORD"),
    database=os.getenv("MYSQL_DATABASE"),
)

cursor = connection.cursor()

query = """
INSERT IGNORE INTO legal_entities
(lei, legal_name, jurisdiction, country,
 registration_status)
VALUES (%s, %s, %s, %s, %s)
"""

columns = [
    "lei",
    "legal_name",
    "jurisdiction",
    "country",
    "registration_status",
]

records = [
    tuple(value or None for value in row)
    for row in df[columns].itertuples(
        index=False, name=None
    )
]

try:
    cursor.executemany(query, records)
    connection.commit()

    print("New records inserted:", cursor.rowcount)

    cursor.execute("SELECT COUNT(*) FROM legal_entities")
    total = cursor.fetchone()[0]

    print("Total entities in database:", total)

finally:
    cursor.close()
    connection.close()