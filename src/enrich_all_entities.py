
import csv
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]

source = ROOT / "reports/corporate_groups.csv"
output = ROOT / "reports/all_entities_enriched.csv"

groups = pd.read_csv(
    source, dtype=str, keep_default_na=False
)

groups = groups.drop_duplicates(subset=["lei"])

if not groups["lei"].str.fullmatch(r"[A-Z0-9]{20}").all():
    raise ValueError("Invalid LEI in corporate groups")

fields = [
    "lei",
    "company",
    "jurisdiction",
    "registered_country",
    "lei_registration_status",
    "source_url",
    "retrieved_at_utc",
]

# Resume from previously saved records
completed = set()

if output.exists():
    previous = pd.read_csv(
        output, dtype=str, keep_default_na=False
    )
    completed = set(previous["lei"])

retry = Retry(
    total=4,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
    respect_retry_after_header=True,
)

session = requests.Session()
session.mount(
    "https://",
    HTTPAdapter(max_retries=retry)
)

with output.open("a", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fields)

    if f.tell() == 0:
        writer.writeheader()

    for row in groups.itertuples(index=False):

        if row.lei in completed:
            continue

        url = (
            "https://api.gleif.org/api/v1/"
            f"lei-records/{row.lei}"
        )

        response = session.get(url, timeout=30)
        response.raise_for_status()

        record = response.json()["data"]

        if record["id"] != row.lei:
            raise ValueError("LEI mismatch")

        attributes = record["attributes"]
        entity = attributes.get("entity") or {}
        address = entity.get("legalAddress") or {}
        registration = attributes.get("registration") or {}

        writer.writerow({
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

        f.flush()
        completed.add(row.lei)

        print(f"[{len(completed)}/{len(groups)}] {row.company}")

        time.sleep(0.3)

session.close()

print("\nEnrichment completed.")
print("Records saved:", len(completed))
print("File:", output)