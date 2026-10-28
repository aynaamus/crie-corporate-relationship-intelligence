
import pandas as pd
import pytest

from src.comparison_rules import classify
from src.disclosure_conflicts import audit_disclosure_claims


PAIRS = [
    ("equity_interest_pct", "49", "49.0%", True),
    ("equity_interest_pct", "49", "49.00", True),
    ("equity_interest_pct", "49", "51", False),
    ("management_controller", "Company A", "company  a", True),
    ("management_controller", "Company A", "Company B", False),
    ("management_controller", "Slovak Gas Holding B.V.",
     "Slovak Gas Holding BV", False),
    ("included_in_ep_consolidation", "YES", " yes ", True),
]


@pytest.mark.parametrize("metric,first,second,equal", PAIRS)
def test_comparison_and_conflict_agree(
    metric, first, second, equal
):
    """YoY 'UNCHANGED' must match same-period 'CONSISTENT'."""

    yoy = classify(pd.Series({
        "metric": metric,
        "value_2024": first,
        "value_2025": second,
        "_merge": "both",
    }))

    audit = audit_disclosure_claims(pd.DataFrame([
        {
            "report_year": "2025",
            "period_end": "2025-09-30",
            "source_id": source_id,
            "entity": "Example Corporation",
            "metric": metric,
            "value": value,
            "source_location": "Page 1",
        }
        for source_id, value in (("1", first), ("2", second))
    ]))

    assert (yoy["comparison_status"] == "UNCHANGED") is equal
    assert (
        audit.iloc[0]["conflict_status"] == "INDEPENDENCE_UNVERIFIED"
    ) is equal
