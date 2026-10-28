
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]

# Read the groups we've already identified
groups = pd.read_csv(
    ROOT / "reports/corporate_groups.csv",
    dtype=str
)

# Start with Group 2: FBN Holdings network
target = groups[groups["group_id"] == "2"]

if target.empty:
    raise ValueError("Group 2 not found")

results = []

with requests.Session() as session:
    for row in target.itertuples(index=False):

        url = (
            "https://api.gleif.org/api/v1/"
            f"lei-records/{row.lei}"
        )

        print(f"Retrieving: {row.company}")

        response = session.get(url, timeout=30)
        response.raise_for_status()

        record = response.json()["data"]

        if record["id"] != row.lei:
            raise ValueError("LEI mismatch")

        attributes = record["attributes"]
        entity = attributes.get("entity") or {}
        address = entity.get("legalAddress") or {}
        registration = attributes.get("registration") or {}

        results.append({
            "lei": row.lei,
            "company": row.company,
            "jurisdiction": entity.get("jurisdiction"),
            "registered_country": address.get("country"),
            "lei_registration_status": registration.get("status"),
            "source_url": url,
            "retrieved_at_utc": datetime.now(
                timezone.utc
            ).isoformat(),
        })

df = pd.DataFrame(results)

output = ROOT / "reports/fbn_entities_enriched.csv"
df.to_csv(output, index=False)

print("\nENRICHED ENTITIES")
print(df[[
    "company",
    "jurisdiction",
    "registered_country"
]].to_string(index=False))

print("\nSaved:", output)
