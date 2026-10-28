
"""
Investigation continuity against the end-to-end database
(corporate_intelligence_e2e only), with the synthetic demonstration files:
a case stays retrievable by its permanent ID, on its original anchor, after
a later GLEIF observation arrives and the relationship stops being active.
"""

import os
import sys
from pathlib import Path

import pandas as pd
import pytest

from src.gleif_import import import_lei2_file, import_rr_file
from src.gleif_observations import canonical_selection
from src.relationship_context import load_relationship_context

from test_mysql_evidence_snapshot import block_default_connections, pytestmark  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "scripts")]

import demo_synthetic_case as demo  # noqa: E402
import investigations  # noqa: E402
import navigation  # noqa: E402

E2E = "corporate_intelligence_e2e"
DIRECT = "IS_DIRECTLY_CONSOLIDATED_BY"


def connect():
    import mysql.connector
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    connection = mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"), port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER"), password=os.getenv("MYSQL_PASSWORD"),
        database=E2E, time_zone="+00:00")
    cursor = connection.cursor()
    cursor.execute("SELECT DATABASE()")
    assert cursor.fetchone()[0] == E2E
    cursor.close()
    connection.rollback()
    return connection


def execute(sql, params=()):
    connection = connect()
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        connection.commit()
        return cursor.lastrowid
    finally:
        cursor.close()
        connection.close()


@pytest.fixture
def demo_files(tmp_path):
    try:
        connection = connect()
    except Exception as error:  # noqa: BLE001
        pytest.skip(f"The end-to-end database is not available: {error}")
    cursor = connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM legal_entities WHERE lei LIKE 'SYNTHDEMO%'")
    present = cursor.fetchone()[0]
    cursor.close()
    connection.close()
    if present:
        pytest.skip("A synthetic demonstration is loaded in the end-to-end database; "
                    "this test does not disturb it.")
    demo.write_files(tmp_path)
    try:
        yield tmp_path
    finally:
        demo.cleanup()


def load(path, dataset):
    importer = import_lei2_file if dataset == "LEI2" else import_rr_file
    return importer(connect, path, canonical_selection(dataset, "ALL_RECORDS"),
                    started_by="synthetic test")


def test_case_is_retrieved_by_id_on_its_original_anchor(demo_files, monkeypatch):
    # The case below is inserted without an owner: a legacy case of the local investigator,
    # visible to that user once owners are recorded.
    monkeypatch.setenv("CRIE_LOCAL_USER", "synthetic analyst")
    monkeypatch.delenv("CRIE_HOSTED", raising=False)
    june = load(demo_files / "synthetic-demo-lei2-june.xml", "LEI2")
    june_rr = load(demo_files / "synthetic-demo-rr-june.xml", "RR")
    assert (june["status"], june_rr["status"]) == ("COMPLETED", "COMPLETED")

    connection = connect()
    relationships, _, _ = load_relationship_context(connection)
    connection.close()
    relationships = pd.DataFrame(relationships)
    anchor = relationships[relationships["parent_name"] == "Synthetic Parent Group AG"].iloc[0]
    child, parent = anchor["child_lei"], anchor["parent_lei"]

    # Opened on the June observation, as the case form saves it.
    case_id = execute(
        "INSERT INTO investigation_cases (child_lei, parent_lei, relationship_type, "
        "snapshot_at_utc) VALUES (%s, %s, %s, %s)",
        (child, parent, DIRECT, anchor["snapshot_at_utc"]))

    # A later observation arrives, then the relationship stops being active.
    load(demo_files / "synthetic-demo-lei2-october.xml", "LEI2")
    load(demo_files / "synthetic-demo-rr-october.xml", "RR")
    connection = connect()
    later, _, _ = load_relationship_context(connection)
    connection.close()
    later = pd.DataFrame(later)
    newest = later[(later["child_lei"] == child) & (later["parent_lei"] == parent)].iloc[0]
    assert newest["snapshot_at_utc"] > anchor["snapshot_at_utc"]
    execute("UPDATE corporate_relationships SET relationship_status = 'INACTIVE' "
            "WHERE child_lei = %s AND parent_lei = %s AND relationship_type = %s",
            (child, parent, DIRECT))

    connection = connect()
    listed, _, _ = load_relationship_context(connection)
    connection.close()
    listed = pd.DataFrame(listed)
    assert listed[(listed["child_lei"] == child)
                  & (listed["parent_lei"] == parent)].empty     # no longer in the dataset view

    cases = investigations.load_cases(connect)
    assert cases["case_id"].tolist() == [case_id]
    assert cases.iloc[0]["current_relationship_status"] == "INACTIVE"
    assert navigation.cases_by_relationship(cases) == {f"{child}|{parent}|{DIRECT}": [case_id]}

    row = investigations.load_anchor(connect, case_id)
    assert row["case_id"] == case_id
    assert pd.Timestamp(row["snapshot_at_utc"]) == pd.Timestamp(anchor["snapshot_at_utc"])
    assert row["source_file"] == "synthetic-demo-rr-june.xml"           # original source
    assert row["anchor_origin"] == "GLEIF_BATCH"
    assert row["anchor_batch_id"] == june_rr["batch_id"]
    assert row["anchor_quarantined_records"] == 1                       # coverage preserved
    assert row["anchor_observations"] == 1
    assert row["child_name"] == "Synthetic Harbour Holdings plc"        # stored identity

    assert investigations.load_anchor(connect, case_id + 10_000) is None
