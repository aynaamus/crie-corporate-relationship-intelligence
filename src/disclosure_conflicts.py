
import json

import pandas as pd

# normalize_claim is re-exported here for existing
# importers (claim_corrections, import_disclosure_claims).
from src.comparison_rules import normalize_claim


REQUIRED_COLUMNS = [
    "report_year",
    "period_end",
    "source_id",
    "entity",
    "metric",
    "value",
    "source_location",
]

GROUP_COLUMNS = [
    "entity",
    "metric",
    "report_year",
    "period_end",
]


def audit_disclosure_claims(facts):
    """Detect conflicting claims about the same reporting period."""

    missing = set(REQUIRED_COLUMNS) - set(facts.columns)

    if missing:
        raise ValueError(
            f"Missing required columns: {sorted(missing)}"
        )

    if facts.empty:
        raise ValueError("No disclosure facts provided")

    df = facts[REQUIRED_COLUMNS].copy()

    for column in REQUIRED_COLUMNS:
        if df[column].isna().any():
            raise ValueError(f"Missing values in {column}")

        df[column] = df[column].astype(str).str.strip()

        if df[column].eq("").any():
            raise ValueError(f"Empty values in {column}")

    df["normalized_value"] = [
        normalize_claim(metric, value)
        for metric, value in zip(
            df["metric"],
            df["value"],
        )
    ]

    results = []

    for key, group in df.groupby(
        GROUP_COLUMNS,
        sort=True,
    ):
        distinct_values = group["normalized_value"].nunique()
        distinct_sources = group["source_id"].nunique()

        if distinct_values > 1:
            status = (
                "CROSS_SOURCE_CONFLICT"
                if distinct_sources > 1
                else "INTRA_SOURCE_CONFLICT"
            )

        elif distinct_sources > 1:
            # No source metadata here, so agreement of several
            # registered sources cannot be called corroboration.
            status = "INDEPENDENCE_UNVERIFIED"

        elif len(group) > 1:
            status = "REPEATED_SOURCE_CLAIMS"

        else:
            status = "SINGLE_CLAIM"

        # Preserve every original claim and its location.
        details = [
            {
                "source_id": row.source_id,
                "value": row.value,
                "source_location": row.source_location,
            }
            for row in group.itertuples(index=False)
        ]

        record = dict(zip(GROUP_COLUMNS, key))

        record.update({
            "claim_count": len(group),
            "source_count": distinct_sources,
            "distinct_value_count": distinct_values,
            "conflict_status": status,
            "claim_details": json.dumps(
                details,
                ensure_ascii=False,
            ),
        })

        results.append(record)

    return pd.DataFrame(results)
