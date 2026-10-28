
from pathlib import Path

import pandas as pd

from src.disclosure_conflicts import audit_disclosure_claims


ROOT = Path(__file__).resolve().parents[1]

input_file = ROOT / "data/derived/disclosure_facts.csv"
output_file = ROOT / "reports/disclosure_conflict_audit.csv"

facts = pd.read_csv(
    input_file,
    dtype=str,
    keep_default_na=False,
)

audit = audit_disclosure_claims(facts)

output_file.parent.mkdir(exist_ok=True)

audit.to_csv(output_file, index=False)

print("\nDISCLOSURE CONFLICT AUDIT")
print(
    audit[
        [
            "entity",
            "metric",
            "report_year",
            "source_count",
            "conflict_status",
        ]
    ].to_string(index=False)
)

print("\nSUMMARY")
print(audit["conflict_status"].value_counts())

print("\nSaved to:", output_file)
