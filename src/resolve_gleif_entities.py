
from pathlib import Path
import xml.etree.ElementTree as ET
import pandas as pd

# Project directory
ROOT = Path(__file__).resolve().parents[1]

relationships_file = ROOT / "gleif_relationships_sample.csv"
entities_file = ROOT / "20261022-gleif-concatenated-file-lei2.xml"
output_file = ROOT / "corporate_relationships_named.csv"

# Step 1: Load relationship records
df = pd.read_csv(relationships_file, dtype=str).fillna("")

print("Total relationships:", len(df))

# Step 2: Select current consolidation relationships
consolidation_types = [
    "IS_DIRECTLY_CONSOLIDATED_BY",
    "IS_ULTIMATELY_CONSOLIDATED_BY",
]

active = df[
    (df["relationship_status"] == "ACTIVE")
    & (df["registration_status"] == "PUBLISHED")
    & (df["relationship_type"].isin(consolidation_types))
].copy()

print("Active published consolidation records:", len(active))

# Step 3: Identify all required LEIs
required_leis = (
    set(active["child_lei"])
    | set(active["parent_lei"])
)

required_leis.discard("")

print("Unique entities to identify:", len(required_leis))

# Step 4: Search the large GLEIF XML incrementally
NS = "{http://www.gleif.org/data/schema/leidata/2016}"
RECORD_TAG = NS + "LEIRecord"

entities = {}
processed = 0

context = ET.iterparse(
    entities_file,
    events=("start", "end")
)

_, root = next(context)

for event, elem in context:
    if event != "end" or elem.tag != RECORD_TAG:
        continue

    processed += 1
    lei = elem.findtext(NS + "LEI")

    if lei in required_leis:
        name = elem.findtext(
            f"{NS}Entity/{NS}LegalName"
        )

        jurisdiction = elem.findtext(
            f"{NS}Entity/{NS}LegalJurisdiction"
        )

        entities[lei] = {
            "name": name,
            "jurisdiction": jurisdiction,
        }

    # Release processed XML records from memory
    root.clear()

    if processed % 500000 == 0:
        print(f"Scanned {processed:,} records")

    if len(entities) == len(required_leis):
        break

print("Entities found:", len(entities))

# Step 5: Join company names to relationships
names = {
    lei: info["name"]
    for lei, info in entities.items()
}

active["child_name"] = active["child_lei"].map(names)
active["parent_name"] = active["parent_lei"].map(names)

# Step 6: Save results
active.to_csv(output_file, index=False)

missing = required_leis - set(entities)

print("\nUnresolved LEIs:", len(missing))
print("Output:", output_file)

print("\nSample relationships:")
print(
    active[
        [
            "child_name",
            "parent_name",
            "relationship_type"
        ]
    ].head(10).to_string(index=False)
)
