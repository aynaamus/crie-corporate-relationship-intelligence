
"""
Investigation ownership, drafts and publication against the end-to-end
database (corporate_intelligence_e2e only), with the synthetic demonstration.
Needs sql/e2e/m24_e2e_investigation_ownership.sql installed.
"""

import hashlib
import sys
from pathlib import Path

import mysql.connector
import pandas as pd
import pytest

from test_mysql_evidence_snapshot import block_default_connections, pytestmark  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "scripts")]

import access  # noqa: E402
import case_management  # noqa: E402
import demo_synthetic_case as demo  # noqa: E402
import investigations  # noqa: E402
import publishing  # noqa: E402

E2E = "corporate_intelligence_e2e"
ALICE = access.User("oidc:test-alice", "Alice")
BOB = access.User("oidc:test-bob", "Bob")


def scalar(sql, params=()):
    connection = demo.connect()
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        return cursor.fetchone()[0]
    finally:
        cursor.close()
        connection.rollback()
        connection.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    try:
        installed = scalar("SELECT COUNT(*) FROM information_schema.COLUMNS WHERE "
                           "TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'investigation_cases' "
                           "AND COLUMN_NAME = 'owner_subject'")
    except Exception as error:  # noqa: BLE001
        pytest.skip(f"The end-to-end database is not available: {error}")
    if not installed:
        pytest.skip("Investigation ownership is not installed in the end-to-end database; "
                    "apply sql/e2e/m24_e2e_investigation_ownership.sql with a privileged account.")
    if scalar("SELECT COUNT(*) FROM legal_entities WHERE lei LIKE 'SYNTHDEMO%'"):
        pytest.skip("A synthetic demonstration is loaded in the end-to-end database; "
                    "this test does not disturb it.")

    monkeypatch.setenv("MYSQL_DATABASE", E2E)
    monkeypatch.setenv("CRIE_RUNS_ROOT", str(tmp_path))
    monkeypatch.delenv(access.LOCAL_USER, raising=False)
    demo.build(tmp_path)
    try:
        yield tmp_path
    finally:
        demo.cleanup()


def as_user(monkeypatch, user):
    monkeypatch.setattr(access, "current_user", lambda: user)


def open_case(monkeypatch, user, row):
    as_user(monkeypatch, user)
    return case_management.save_case(row, "OPEN", "NOT_ASSESSED", f"Opened by {user.name}.", "")


def demo_row():
    from src.relationship_context import load_relationship_context

    connection = case_management.connect_db()
    try:
        rows, _, _ = load_relationship_context(connection)
    finally:
        connection.close()
    return pd.Series(next(r for r in rows if r["parent_name"] == "Synthetic Parent Group AG"))


def test_owners_drafts_and_publication(world, monkeypatch):
    # The example is published, owned by the system owner, and nobody's draft.
    published = publishing.load_published(case_management.connect_db)
    assert published["title"].tolist() == [demo.PUBLICATION_TITLE]
    demo_case = int(published.iloc[0]["case_id"])

    # Two users each open their own case on the example's relationship and evidence row.
    row = demo_row()
    alice_case = open_case(monkeypatch, ALICE, row)
    bob_case = open_case(monkeypatch, BOB, row)
    assert len({demo_case, alice_case, bob_case}) == 3

    as_user(monkeypatch, ALICE)
    assert investigations.load_cases(case_management.connect_db)["case_id"].tolist() == [alice_case]
    assert case_management.get_relationship_cases(row) == [alice_case]
    as_user(monkeypatch, BOB)
    assert investigations.load_cases(case_management.connect_db)["case_id"].tolist() == [bob_case]
    as_user(monkeypatch, None)
    assert investigations.load_cases(case_management.connect_db).empty

    # Nobody changes someone else's case, or the example.
    connection = case_management.connect_db()
    try:
        access.require_write(connection, alice_case, ALICE)
        for case_id in (alice_case, demo_case):
            with pytest.raises(access.AccessDenied):
                access.require_write(connection, case_id, BOB)
        with pytest.raises(access.AccessDenied):
            access.require_write(connection, demo_case, ALICE)
    finally:
        connection.rollback()
        connection.close()
    as_user(monkeypatch, BOB)
    with pytest.raises(access.AccessDenied):
        case_management.save_case(pd.Series({**row.to_dict(), "case_id": alice_case}),
                                  "CLOSED", "NOT_ASSESSED", "", "")
    assert scalar("SELECT review_status FROM investigation_cases WHERE case_id = %s",
                  (alice_case,)) == "OPEN"

    # Drafts are never published.
    assert publishing.load_published(case_management.connect_db)["case_id"].tolist() == [demo_case]

    # Alice records a claim, publishes a report version, then a later one; the first is unchanged.
    import datetime

    import disclosure_panel

    as_user(monkeypatch, ALICE)
    connection = case_management.connect_db()
    cursor = connection.cursor()
    access.require_write(connection, alice_case)
    cursor.execute("INSERT INTO case_sources (case_id, document_title, source_url, "
                   "evidence_assessment) VALUES (%s, 'Synthetic filing', "
                   "'https://example.invalid/alice', 'NOT_REVIEWED')", (alice_case,))
    source_id = cursor.lastrowid
    connection.commit()
    cursor.close()
    connection.close()
    disclosure_panel.record_claim(case_management.connect_db, alice_case, source_id,
                                  "Synthetic Harbour Holdings plc", "parent_interest_pct", 2025,
                                  datetime.date(2025, 12, 31), "60", "Section 4")
    first = publishing.create_report_version(case_management.connect_db, alice_case, 2024, 2025)
    first_files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in (world / f"case_{alice_case}" / first).iterdir()}
    runs = {r["run_id"]: r for r in _runs(alice_case)}
    publishing.publish(case_management.connect_db, alice_case, runs[first], "Alice's findings",
                       "Synthetic summary.", "Alice")
    second = publishing.create_report_version(case_management.connect_db, alice_case, 2024, 2025)
    runs = {r["run_id"]: r for r in _runs(alice_case)}
    publishing.publish(case_management.connect_db, alice_case, runs[second], "Alice's findings, v2",
                       "Synthetic summary, revised.", "Alice")
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (world / f"case_{alice_case}" / first).iterdir()} == first_files

    current = publishing.load_published(case_management.connect_db).set_index("case_id")
    assert current.loc[alice_case, "run_id"] == second
    assert current.loc[alice_case, "earlier"] == 1
    assert [p["run_id"] for p in publishing.case_publications(
        case_management.connect_db, alice_case)] == [second, first]

    # Bob cannot publish Alice's case; nobody can publish another case's run.
    as_user(monkeypatch, BOB)
    with pytest.raises(access.AccessDenied):
        publishing.publish(case_management.connect_db, alice_case, runs[second], "Bob's", "x", "Bob")

    # The database refuses the same, whatever the application does.
    connection = demo.connect()
    cursor = connection.cursor()
    try:
        for case_id, run_id, by in ((alice_case, second, BOB.subject),
                                    (bob_case, second, BOB.subject)):
            with pytest.raises(mysql.connector.Error, match="owner can publish|does not belong"):
                cursor.execute("INSERT INTO investigation_publications (case_id, run_id, title, "
                               "summary, author_name, published_by) VALUES (%s, %s, 't', 's', "
                               "'a', %s)", (case_id, run_id, by))
        with pytest.raises(mysql.connector.Error, match="append-only"):
            cursor.execute("UPDATE investigation_publications SET title = 'x' WHERE case_id = %s",
                           (alice_case,))
        with pytest.raises(mysql.connector.Error, match="owner cannot be changed"):
            cursor.execute("UPDATE investigation_cases SET owner_subject = %s WHERE case_id = %s",
                           (BOB.subject, alice_case))
    finally:
        cursor.close()
        connection.rollback()
        connection.close()


def _runs(case_id):
    from src.report_registry import list_registered_runs

    connection = case_management.connect_db()
    try:
        return list_registered_runs(connection, case_id)
    finally:
        connection.rollback()
        connection.close()



def test_guest_sandboxes(world, monkeypatch):
    import datetime

    import disclosure_panel

    guest = access.User("guest:test-sandbox", "Guest")
    row = demo_row()
    case_id = open_case(monkeypatch, guest, row)

    # A guest records a claim on their own case, through the same path as the form.
    connection = case_management.connect_db()
    cursor = connection.cursor()
    cursor.execute("INSERT INTO case_sources (case_id, document_title, source_url, "
                   "evidence_assessment) VALUES (%s, 'Synthetic filing', "
                   "'https://example.invalid/f', 'NOT_REVIEWED')", (case_id,))
    source_id = cursor.lastrowid
    connection.commit()
    cursor.close()
    connection.close()
    claim_id = disclosure_panel.record_claim(
        case_management.connect_db, case_id, source_id, "Synthetic Harbour Holdings plc",
        "parent_interest_pct", 2025, datetime.date(2025, 12, 31), "60", "Section 4")
    assert scalar("SELECT normalized_value FROM disclosure_claims WHERE claim_id = %s",
                  (claim_id,)) == "60"
    as_user(monkeypatch, BOB)
    with pytest.raises(access.AccessDenied):
        disclosure_panel.record_claim(case_management.connect_db, case_id, source_id, "X",
                                      "metric", 2025, datetime.date(2025, 12, 31), "1", "p")

    # Only the guest sees the sandbox case; a guest cannot publish.
    as_user(monkeypatch, guest)
    assert investigations.load_cases(case_management.connect_db)["case_id"].tolist() == [case_id]
    with pytest.raises(access.AccessDenied, match="sandbox"):
        publishing.publish(case_management.connect_db, case_id, {"run_id": "x"}, "t", "s", "a")

    # A sandbox holds at most GUEST_CASE_LIMIT investigations (two here: the demonstration
    # has only three evidence rows to open cases on).
    from src.relationship_context import load_relationship_context

    connection = case_management.connect_db()
    rows, _, _ = load_relationship_context(connection)
    connection.close()
    other = pd.Series(next(r for r in rows if r["parent_name"] != "Synthetic Parent Group AG"))
    june = pd.Series({**row.to_dict(), "snapshot_at_utc": pd.Timestamp("2026-06-01 09:00")})
    monkeypatch.setattr(access, "GUEST_CASE_LIMIT", 2)
    open_case(monkeypatch, guest, other)
    with pytest.raises(access.AccessDenied, match="can hold 2 investigations"):
        open_case(monkeypatch, guest, june)
    assert scalar("SELECT COUNT(*) FROM investigation_cases WHERE owner_subject = %s",
                  (guest.subject,)) == 2

    # Guest cleanup removes guests' cases only, never the published example.
    demo_case = int(publishing.load_published(case_management.connect_db).iloc[0]["case_id"])
    connection = demo.connect()
    cursor = connection.cursor()
    cursor.execute("UPDATE investigation_cases SET created_at = UTC_TIMESTAMP() - INTERVAL 3 HOUR "
                   "WHERE owner_subject LIKE 'guest:%'")
    connection.commit()
    cursor.close()
    connection.close()
    demo.cleanup_guests(2)
    assert scalar("SELECT COUNT(*) FROM investigation_cases WHERE owner_subject LIKE 'guest:%'") == 0
    assert scalar("SELECT COUNT(*) FROM investigation_cases WHERE case_id = %s", (demo_case,)) == 1
