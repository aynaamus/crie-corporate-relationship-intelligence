
import os
from pathlib import Path

import mysql.connector
import pandas as pd
from dotenv import load_dotenv

from src.comparison_rules import normalize_claim


ROOT = Path(__file__).resolve().parents[1]

COLUMNS = [
    "claim_id",
    "report_year",
    "period_end",
    "source_id",
    "entity",
    "metric",
    "value",
    "normalized_value",
    "source_location",
]

# One comparable value per entity, metric, and year.
DUPLICATE_KEYS = [
    "report_year",
    "entity",
    "metric",
]


def _connect():
    """Open a new connection using the project .env settings."""

    load_dotenv(ROOT / ".env")

    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv(
            "MYSQL_DATABASE",
            "corporate_intelligence",
        ),
    )


def _fetch_records(connection, case_id, years):
    cursor = connection.cursor(dictionary=True)

    placeholders = ", ".join(["%s"] * len(years))

    try:
        cursor.execute(
            f"""
            SELECT
                c.claim_id,
                c.report_year,
                c.period_end,
                c.source_id,
                c.entity_name AS entity,
                c.metric,
                c.value_text AS value,
                c.normalized_value,
                c.source_location

            FROM v_current_disclosure_claims c

            JOIN case_sources s
                ON s.source_id = c.source_id

            WHERE s.case_id = %s
              AND c.report_year IN ({placeholders})

            ORDER BY
                c.report_year,
                c.entity_name,
                c.metric,
                c.claim_id
            """,
            (case_id, *years),
        )

        return cursor.fetchall()

    finally:
        cursor.close()


def load_current_disclosures(
    case_id=1,
    years=(2024, 2025),
    strict=True,
    connection=None,
):
    """
    Retrieve current, non-superseded disclosure claims
    for an investigation case and reporting years.

    strict=True raises when more than one current claim
    covers the same entity, metric, and year.
    strict=False returns every current claim so that
    competing claims can be reconciled later; none are
    discarded or chosen between.

    Connection ownership:
    - connection=None: this function opens its own
      connection, reads inside a read-only transaction,
      ends that transaction, and always closes it.
    - connection supplied: the caller owns it and its
      transaction lifecycle. It is not closed, committed,
      rolled back, or given a new transaction here.
    """

    years = tuple(years)

    if not years or not all(
        isinstance(year, int) and not isinstance(year, bool)
        for year in years
    ):
        raise ValueError(
            "years must be a non-empty sequence of integers"
        )

    if connection is not None:
        records = _fetch_records(connection, case_id, years)

    else:
        owned = _connect()

        try:
            owned.start_transaction(readonly=True)

            try:
                records = _fetch_records(owned, case_id, years)
            finally:
                # Ends the read-only transaction; nothing
                # was written.
                owned.rollback()

        finally:
            owned.close()

    if not records:
        raise ValueError(
            f"No current disclosure claims "
            f"found for investigation case {case_id}."
        )

    return prepare_claims_frame(records, strict=strict)


def prepare_claims_frame(records, strict=True):
    """
    Validate claim records (dicts with COLUMNS keys) and
    return them as the string DataFrame used by the
    comparison and reconciliation engines.
    """

    df = pd.DataFrame(records, columns=COLUMNS)

    # Match the string format expected by
    # the existing comparison engine.
    for column in COLUMNS:
        if df[column].isna().any():
            raise ValueError(
                f"NULL disclosure field: {column}"
            )

        df[column] = (
            df[column].astype(str).str.strip()
        )

        if df[column].eq("").any():
            raise ValueError(
                f"Empty disclosure field: {column}"
            )

    # Stored normalized values must agree with the
    # shared normalization rules; never swap silently.
    expected = [
        normalize_claim(metric, value)
        for metric, value in zip(df["metric"], df["value"])
    ]

    mismatched = df[df["normalized_value"] != expected]

    if not mismatched.empty:
        raise ValueError(
            "Stored normalized values differ from current "
            "normalization rules for claim IDs: "
            f"{mismatched['claim_id'].tolist()}"
        )

    if strict:
        # Do not silently select one document when
        # multiple current claims cover the same metric.
        duplicates = df[
            df.duplicated(DUPLICATE_KEYS, keep=False)
        ]

        if not duplicates.empty:
            details = duplicates[
                [
                    "claim_id",
                    "source_id",
                    "report_year",
                    "entity",
                    "metric",
                    "value",
                ]
            ].to_string(index=False)

            raise ValueError(
                "Multiple current claims found for "
                "the same entity, metric, and year.\n"
                "Review these claims before comparing:\n"
                + details
            )

    return df.reset_index(drop=True)
