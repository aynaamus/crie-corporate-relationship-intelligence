
import csv
import hashlib
import json
import os

from datetime import date
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv

from src.disclosure_conflicts import normalize_claim


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

INPUT_FILE = ROOT / "data/derived/disclosure_facts.csv"

CASE_ID = 1

REQUIRED = [
    "report_year",
    "period_end",
    "source_id",
    "entity",
    "metric",
    "value",
    "source_location",
]


def prepare_claims():
    """Read and validate claims before writing to MySQL."""

    with INPUT_FILE.open(
        encoding="utf-8-sig",
        newline="",
    ) as file:
        reader = csv.DictReader(file)

        if not reader.fieldnames:
            raise ValueError("Disclosure CSV has no header")

        missing = set(REQUIRED) - set(reader.fieldnames)

        if missing:
            raise ValueError(
                f"Missing CSV columns: {sorted(missing)}"
            )

        records = list(reader)

    if not records:
        raise ValueError("Disclosure CSV is empty")

    prepared = []

    for line_number, record in enumerate(records, start=2):
        fields = {
            key: str(record[key]).strip()
            for key in REQUIRED
        }

        if any(not value for value in fields.values()):
            raise ValueError(
                f"Missing claim field on line {line_number}"
            )

        try:
            source_id = int(fields["source_id"])
            year = int(fields["report_year"])
            period = date.fromisoformat(
                fields["period_end"]
            )
        except ValueError as error:
            raise ValueError(
                f"Invalid source, year, or date "
                f"on line {line_number}"
            ) from error

        if source_id <= 0 or not 1900 <= year <= 2100:
            raise ValueError(
                f"Invalid source ID or year "
                f"on line {line_number}"
            )

        normalized = normalize_claim(
            fields["metric"],
            fields["value"],
        )

        fingerprint_data = [
            source_id,
            fields["entity"],
            fields["metric"],
            year,
            period.isoformat(),
            fields["value"],
            fields["source_location"],
        ]

        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_data,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        prepared.append((
            source_id,
            fields["entity"],
            fields["metric"],
            year,
            period,
            fields["value"],
            normalized,
            fields["source_location"],
            fingerprint,
        ))

    return prepared


def main():
    claims = prepare_claims()

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

    cursor = connection.cursor()

    try:
        connection.start_transaction()

        # Only allow source documents belonging to this case.
        cursor.execute(
            """
            SELECT source_id
            FROM case_sources
            WHERE case_id = %s
            """,
            (CASE_ID,),
        )

        allowed_sources = {
            int(row[0]) for row in cursor.fetchall()
        }

        referenced_sources = {
            claim[0] for claim in claims
        }

        invalid_sources = referenced_sources - allowed_sources

        if invalid_sources:
            raise ValueError(
                "Sources do not belong to this case: "
                f"{sorted(invalid_sources)}"
            )

        inserted = 0
        existing = 0

        for claim in claims:
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
                ON DUPLICATE KEY UPDATE
                    claim_fingerprint = claim_fingerprint
                """,
                claim,
            )

            if cursor.rowcount == 1:
                inserted += 1
            else:
                existing += 1

        connection.commit()

        print("Disclosure claim import completed")
        print("New claims:", inserted)
        print("Already registered:", existing)
        print("Total input claims:", len(claims))

    except Exception:
        connection.rollback()
        raise

    finally:
        cursor.close()
        connection.close()


if __name__ == "__main__":
    main()
