
import csv
import zipfile
from pathlib import Path
from contextlib import ExitStack
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]

files = [
    p for p in ROOT.iterdir()
    if p.is_file()
    and "rr" in p.name.lower()
    and "gleif" in p.name.lower()
    and p.suffix.lower() in (".zip", ".xml")
]

if len(files) != 1:
    raise SystemExit(
        f"Expected one GLEIF RR file; found {len(files)}: {files}"
    )

source = files[0]
output = ROOT / "gleif_relationships_sample.csv"

NS = "{http://www.gleif.org/data/schema/rr/2016}"
RECORD = NS + "RelationshipRecord"

fields = [
    "child_lei",
    "parent_lei",
    "relationship_type",
    "relationship_status",
    "registration_status",
]

count = 0

with ExitStack() as stack:
    if source.suffix.lower() == ".zip":
        archive = stack.enter_context(zipfile.ZipFile(source))
        xml_files = [
            n for n in archive.namelist()
            if n.lower().endswith(".xml")
        ]
        if not xml_files:
            raise ValueError("No XML file found in ZIP")
        stream = stack.enter_context(archive.open(xml_files[0]))
    else:
        stream = stack.enter_context(source.open("rb"))

    file = stack.enter_context(
        output.open("w", newline="", encoding="utf-8")
    )
    writer = csv.DictWriter(file, fieldnames=fields)
    writer.writeheader()

    context = ET.iterparse(stream, events=("start", "end"))
    _, root = next(context)

    for event, elem in context:
        if event != "end" or elem.tag != RECORD:
            continue

        def value(path):
            return elem.findtext(path)

        writer.writerow({
            "child_lei": value(
                f"{NS}Relationship/{NS}StartNode/{NS}NodeID"
            ),
            "parent_lei": value(
                f"{NS}Relationship/{NS}EndNode/{NS}NodeID"
            ),
            "relationship_type": value(
                f"{NS}Relationship/{NS}RelationshipType"
            ),
            "relationship_status": value(
                f"{NS}Relationship/{NS}RelationshipStatus"
            ),
            "registration_status": value(
                f"{NS}Registration/{NS}RegistrationStatus"
            ),
        })

        count += 1
        root.clear()

        if count >= 250:
            break

print(f"Extracted {count} relationships")
print(f"Saved to: {output}")
