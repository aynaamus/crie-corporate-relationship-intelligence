
import csv
import xml.etree.ElementTree as ET
from pathlib import Path

source = Path("20261022-gleif-concatenated-file-lei2.xml")
output = Path("gleif_sample_1000.csv")

namespace = "{http://www.gleif.org/data/schema/leidata/2016}"
record_tag = namespace + "LEIRecord"

fields = [
    "lei",
    "legal_name",
    "jurisdiction",
    "country",
    "registration_status"
]

count = 0

with output.open("w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fields)
    writer.writeheader()

    context = ET.iterparse(source, events=("start", "end"))
    _, root = next(context)

    for event, elem in context:
        if event != "end" or elem.tag != record_tag:
            continue

        def get_text(path):
            value = elem.findtext(path)
            return value.strip() if value else None

        writer.writerow({
            "lei": get_text(namespace + "LEI"),
            "legal_name": get_text(
                f"{namespace}Entity/{namespace}LegalName"
            ),
            "jurisdiction": get_text(
                f"{namespace}Entity/{namespace}LegalJurisdiction"
            ),
            "country": get_text(
                f"{namespace}Entity/{namespace}LegalAddress/{namespace}Country"
            ),
            "registration_status": get_text(
                f"{namespace}Registration/{namespace}RegistrationStatus"
            )
        })

        count += 1
        root.clear()

        if count >= 1000:
            break

print(f"Extracted {count} records")
print(f"Saved to: {output.resolve()}")