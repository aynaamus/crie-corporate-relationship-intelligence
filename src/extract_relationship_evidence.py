
import json
import zipfile
from contextlib import ExitStack
from pathlib import Path
import xml.etree.ElementTree as ET

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

# Previously verified relationships
relationships = pd.read_csv(
    ROOT / "corporate_relationships_named.csv",
    dtype=str,
    keep_default_na=False
)

# Identify relationships by both LEIs and type
keys = set(
    zip(
        relationships["child_lei"],
        relationships["parent_lei"],
        relationships["relationship_type"]
    )
)

# Find the original RR-CDF file
candidates = [
    p for p in ROOT.iterdir()
    if p.is_file()
    and "gleif" in p.name.lower()
    and "rr" in p.name.lower()
    and p.suffix.lower() in {".xml", ".zip"}
]

if len(candidates) != 1:
    raise ValueError(
        f"Expected one RR-CDF file, found: {candidates}"
    )

source = candidates[0]
output = ROOT / "reports/relationship_evidence.csv"

NS = "{http://www.gleif.org/data/schema/rr/2016}"

found = {}
content_date = None

with ExitStack() as stack:

    if source.suffix.lower() == ".zip":
        archive = stack.enter_context(
            zipfile.ZipFile(source)
        )
        xml_files = [
            name for name in archive.namelist()
            if name.lower().endswith(".xml")
        ]

        if len(xml_files) != 1:
            raise ValueError(
                f"Expected one XML in archive: {xml_files}"
            )

        stream = stack.enter_context(
            archive.open(xml_files[0])
        )
    else:
        stream = stack.enter_context(
            source.open("rb")
        )

    context = ET.iterparse(
        stream,
        events=("start", "end")
    )

    records_parent = None

    for event, elem in context:

        if event == "start":
            if elem.tag == NS + "RelationshipRecords":
                records_parent = elem
            continue

        if elem.tag == NS + "Header":
            content_date = elem.findtext(
                NS + "ContentDate"
            )
            elem.clear()
            continue

        if elem.tag != NS + "RelationshipRecord":
            continue

        def get(path):
            return elem.findtext(path) or ""

        child = get(
            f"{NS}Relationship/{NS}StartNode/{NS}NodeID"
        )

        parent = get(
            f"{NS}Relationship/{NS}EndNode/{NS}NodeID"
        )

        relationship_type = get(
            f"{NS}Relationship/{NS}RelationshipType"
        )

        key = (child, parent, relationship_type)

        if key in keys:

            status = get(
                f"{NS}Relationship/{NS}RelationshipStatus"
            )

            registration_status = get(
                f"{NS}Registration/{NS}RegistrationStatus"
            )

            if (
                status == "ACTIVE"
                and registration_status == "PUBLISHED"
            ):

                periods = []

                for period in elem.findall(
                    f"{NS}Relationship/"
                    f"{NS}RelationshipPeriods/"
                    f"{NS}RelationshipPeriod"
                ):
                    periods.append({
                        "type": period.findtext(
                            NS + "PeriodType"
                        ),
                        "start": period.findtext(
                            NS + "StartDate"
                        ),
                        "end": period.findtext(
                            NS + "EndDate"
                        ),
                    })

                if key in found:
                    raise ValueError(
                        f"Duplicate matching record: {key}"
                    )

                found[key] = {
                    "child_lei": child,
                    "parent_lei": parent,
                    "relationship_type": relationship_type,
                    "relationship_status": status,
                    "registration_status": registration_status,
                    "initial_registration_date": get(
                        f"{NS}Registration/"
                        f"{NS}InitialRegistrationDate"
                    ),
                    "last_update_date": get(
                        f"{NS}Registration/"
                        f"{NS}LastUpdateDate"
                    ),
                    "next_renewal_date": get(
                        f"{NS}Registration/"
                        f"{NS}NextRenewalDate"
                    ),
                    "managing_lou": get(
                        f"{NS}Registration/"
                        f"{NS}ManagingLOU"
                    ),
                    "validation_sources": get(
                        f"{NS}Registration/"
                        f"{NS}ValidationSources"
                    ),
                    "validation_documents": get(
                        f"{NS}Registration/"
                        f"{NS}ValidationDocuments"
                    ),
                    "validation_reference": get(
                        f"{NS}Registration/"
                        f"{NS}ValidationReference"
                    ),
                    "periods_json": json.dumps(periods),
                    "source_file": source.name,
                    "file_content_date": content_date,
                }

        # Free processed records from memory
        if records_parent is not None:
            records_parent.clear()
        else:
            elem.clear()

        if len(found) == len(keys):
            break

missing = keys - set(found)

if missing:
    raise ValueError(
        f"Could not resolve {len(missing)} relationships. "
        f"First missing key: {next(iter(missing))}"
    )

df = pd.DataFrame([
    found[key] for key in sorted(keys)
])

output.parent.mkdir(exist_ok=True)

df.to_csv(output, index=False)

print("Expected relationships:", len(keys))
print("Evidence records extracted:", len(df))
print("Missing relationships:", len(missing))

print("\nValidation sources:")
print(df["validation_sources"].value_counts())

print("\nValidation documents:")
print(df["validation_documents"].value_counts())

print("\nSource snapshot:", content_date)
print("Saved:", output)
