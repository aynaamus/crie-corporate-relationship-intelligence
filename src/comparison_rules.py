
from decimal import Decimal, InvalidOperation

import pandas as pd


def parse_percentage(value):
    """Convert a percentage string into a Decimal."""
    cleaned = str(value).strip().removesuffix("%").strip()

    try:
        number = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(
            f"Invalid percentage: {value}"
        ) from exc

    if not number.is_finite() or not 0 <= number <= 100:
        raise ValueError(
            f"Percentage outside valid range: {value}"
        )

    return number


def normalize_claim(metric, value):
    """
    Normalize a value before comparing claims.

    Shared by year-over-year comparison and
    same-period conflict detection.
    """

    if metric.endswith("_pct"):
        number = parse_percentage(value)
        return format(number.normalize(), "f")

    # Normalize spacing and capitalization, not meaning.
    # Different legal names remain different values.
    return " ".join(str(value).split()).casefold()


def classify(
    row,
    previous="value_2024",
    current="value_2025",
):
    """Compare a disclosure across two reporting periods."""

    if row["_merge"] != "both":
        return pd.Series({
            "comparison_status": "MISSING_PERIOD",
            "change_percentage_points": "",
        })

    old = str(row[previous]).strip()
    new = str(row[current]).strip()

    if row["metric"].endswith("_pct"):
        old_num = parse_percentage(old)
        new_num = parse_percentage(new)

        difference = new_num - old_num

        return pd.Series({
            "comparison_status": (
                "UNCHANGED" if difference == 0 else "CHANGED"
            ),
            "change_percentage_points": format(
                difference.normalize(), "f"
            ),
        })

    metric = row["metric"]

    return pd.Series({
        "comparison_status": (
            "UNCHANGED"
            if normalize_claim(metric, old)
            == normalize_claim(metric, new)
            else "CHANGED"
        ),
        "change_percentage_points": "",
    })
