
"""
Year-over-year comparison of current disclosure claims.

Supported entry point:
    python -m src.compare_disclosures

Direct invocation (python src/compare_disclosures.py)
is kept for compatibility.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Direct-script invocation puts src/ (not the project
# root) on sys.path; add the root before src imports.
if not __package__:
    sys.path.insert(0, str(ROOT))

from src.comparison_rules import classify  # noqa: E402
from src.load_current_disclosures import (  # noqa: E402
    load_current_disclosures,
)

DEFAULT_OUTPUT = ROOT / "reports/disclosure_comparison.csv"

REQUIRED = {
    "report_year",
    "period_end",
    "source_id",
    "entity",
    "metric",
    "value",
    "source_location",
}

# Each metric should appear once per entity and year
KEYS = ["report_year", "entity", "metric"]


def _period_columns(df, year, include_claim_id):
    """Select one period's claims with year-suffixed names."""

    renames = {
        "value": f"value_{year}",
        "source_id": f"source_{year}",
        "period_end": f"period_end_{year}",
        "source_location": f"location_{year}",
    }

    if include_claim_id:
        renames["claim_id"] = f"claim_id_{year}"

    period = df[df["report_year"] == year]

    return period[
        ["entity", "metric", *renames]
    ].rename(columns=renames)


def build_comparison(
    claims,
    previous_year=2024,
    current_year=2025,
):
    """
    Compare the same metric across two reporting years.

    Raises instead of choosing when a metric has more
    than one claim for an entity and year.
    """

    previous_year = str(previous_year)
    current_year = str(current_year)

    if previous_year == current_year:
        raise ValueError("Comparison years must differ")

    if not REQUIRED.issubset(claims.columns):
        raise ValueError("Missing required columns")

    df = claims.copy()
    df["report_year"] = df["report_year"].astype(str)

    if df.duplicated(KEYS).any():
        raise ValueError(
            "Duplicate disclosure facts found"
        )

    if not df["report_year"].isin(
        [previous_year, current_year]
    ).all():
        raise ValueError("Unexpected reporting year")

    if df[["source_id", "entity", "metric", "value"]].eq("").any().any():
        raise ValueError("Required fact fields cannot be empty")

    include_claim_id = "claim_id" in df.columns

    previous = _period_columns(
        df, previous_year, include_claim_id
    )
    current = _period_columns(
        df, current_year, include_claim_id
    )

    comparison = previous.merge(
        current,
        on=["entity", "metric"],
        how="outer",
        validate="one_to_one",
        indicator=True,
    )

    comparison = comparison.fillna("")

    comparison[
        ["comparison_status", "change_percentage_points"]
    ] = comparison.apply(
        classify,
        axis=1,
        previous=f"value_{previous_year}",
        current=f"value_{current_year}",
    )

    comparison = comparison.drop(columns=["_merge"])

    # Claim IDs are appended last so existing column
    # positions are unchanged for report generation.
    if include_claim_id:
        claim_columns = [
            f"claim_id_{previous_year}",
            f"claim_id_{current_year}",
        ]
        comparison = comparison[
            [c for c in comparison.columns if c not in claim_columns]
            + claim_columns
        ]

    return comparison.sort_values(
        ["entity", "metric"]
    ).reset_index(drop=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Compare current disclosure claims "
        "across two reporting years."
    )
    parser.add_argument("--case-id", type=int, default=1)
    parser.add_argument("--previous-year", type=int, default=2024)
    parser.add_argument("--current-year", type=int, default=2025)
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT
    )
    args = parser.parse_args(argv)

    # Load current disclosure claims directly from MySQL.
    df = load_current_disclosures(
        case_id=args.case_id,
        years=(args.previous_year, args.current_year),
    )

    print(
        f"Loaded {len(df)} current disclosure "
        "claims from MySQL."
    )

    comparison = build_comparison(
        df,
        previous_year=args.previous_year,
        current_year=args.current_year,
    )

    # Save reproducible results
    args.output.parent.mkdir(parents=True, exist_ok=True)

    comparison.to_csv(args.output, index=False)

    print("\nYEAR-OVER-YEAR COMPARISON")
    print(
        comparison[
            [
                "entity",
                "metric",
                f"value_{args.previous_year}",
                f"value_{args.current_year}",
                "comparison_status",
            ]
        ].to_string(index=False)
    )

    print("\nSUMMARY")
    print(comparison["comparison_status"].value_counts())

    print("\nSaved to:", args.output)


if __name__ == "__main__":
    main()
