
"""
Claim-subject identity mappings against the real test database
(corporate_intelligence_test only). Entities and LEIs are synthetic
(well-formed, valid check digits). Raw SQL shows the database itself
enforces each rule.
"""

import threading
import uuid

import pytest
from mysql.connector import errors

from src.claim_subjects import MappingError, fetch_mappings, record_mapping
from src.evidence_snapshot import as_version, snapshot_sha256

from test_mysql_evidence_snapshot import (  # noqa: F401
    block_default_connections,
    connect,
    pytestmark,
)
from test_mysql_report_registry import registry_db, seed, snapshot_now  # noqa: F401

SUBJECT = "Fictional Harbour Holdings plc"


def synthetic_lei(prefix):
    base = (prefix + "0" * 18)[:18]
    check = 98 - int("".join(str(int(c, 36)) for c in base + "00")) % 97
    return f"{base}{check:02d}"


LEI_A, LEI_B = synthetic_lei("SYNTHMAPA"), synthetic_lei("SYNTHMAPB")


@pytest.fixture
def mapped_db(registry_db):
    connection, fixtures, run_ids = registry_db
    cursor = connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
                   "AND TABLE_NAME = 'claim_subject_mappings'")
    installed = cursor.fetchone()[0]
    connection.rollback()
    if not installed:
        cursor.close()
        pytest.skip("Claim-subject mappings are not installed in the test database; apply "
                    "sql/test_schema_upgrade_m23b2a.sql with a privileged account "
                    "(test database only).")

    case_id, a, b, c1, c2 = seed(fixtures)
    cursor.execute("INSERT INTO legal_entities (lei, legal_name) VALUES (%s, %s), (%s, %s)",
                   (LEI_A, "Synthetic Harbour Holdings plc", LEI_B, "Synthetic Harbour Group"))
    connection.commit()
    cursor.close()
    extra = {"cases": [], "sources": []}
    try:
        yield connection, fixtures, case_id, a, b, c1, extra
    finally:
        connection.rollback()
        cleanup = connection.cursor()
        cases = [case_id, *extra["cases"]]
        slots = ", ".join(["%s"] * len(cases))
        cleanup.execute("SET @crie_mapping_maintenance = 1")
        cleanup.execute(f"DELETE FROM claim_subject_mappings WHERE case_id IN ({slots}) "
                        "ORDER BY mapping_id DESC", cases)
        cleanup.execute("SET @crie_mapping_maintenance = 0")
        for source_id in extra["sources"]:
            cleanup.execute("DELETE FROM disclosure_claims WHERE source_id = %s", (source_id,))
            cleanup.execute("DELETE FROM case_sources WHERE source_id = %s", (source_id,))
        for other in extra["cases"]:
            cleanup.execute("DELETE FROM investigation_cases WHERE case_id = %s", (other,))
        cleanup.execute("DELETE FROM legal_entities WHERE lei IN (%s, %s)", (LEI_A, LEI_B))
        connection.commit()
        cleanup.close()


def mapped(**overrides):
    fields = dict(subject_text=SUBJECT, status="MAPPED", basis="DOCUMENTED",
                  rationale="Synthetic: register entry names this entity",
                  recorded_by="synthetic reviewer", lei=LEI_A, name_type="LEGAL_NAME",
                  evidence_reference="Synthetic register", evidence_location="Entry 1",
                  limitations="Synthetic fixture only")
    fields.update(overrides)
    return fields


def raw(connection, case_id, subject=SUBJECT, status="UNRESOLVED", supersedes=None, **columns):
    values = {"case_id": case_id, "subject_text": subject, "status": status,
              "basis": "INVESTIGATOR_JUDGEMENT", "rationale": "Synthetic raw row",
              "recorded_by": "synthetic", "supersedes_mapping_id": supersedes,
              "submission_key": uuid.uuid4().hex * 2, **columns}
    cursor = connection.cursor()
    try:
        cursor.execute(f"INSERT INTO claim_subject_mappings ({', '.join(values)}) "
                       f"VALUES ({', '.join(['%s'] * len(values))})", tuple(values.values()))
        connection.commit()
        return cursor.lastrowid
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def other_investigation(connection, extra, subject):
    cursor = connection.cursor()
    cursor.execute("INSERT INTO investigation_cases (case_name) VALUES ('Synthetic other case')")
    case_id = cursor.lastrowid
    extra["cases"].append(case_id)
    cursor.execute("INSERT INTO case_sources (case_id, document_title) VALUES (%s, 'Other')", (case_id,))
    source_id = cursor.lastrowid
    extra["sources"].append(source_id)
    cursor.execute(
        "INSERT INTO disclosure_claims (source_id, entity_name, metric, report_year, period_end, "
        "value_text, normalized_value, source_location, claim_fingerprint) "
        "VALUES (%s, %s, 'harbour_interest_pct', 2025, '2025-12-31', '49', '49', 'Page 1', %s)",
        (source_id, subject, uuid.uuid4().hex * 2))
    connection.commit()
    cursor.close()
    return case_id, source_id


def test_confirmed_mapping_leaves_the_claims_unchanged(mapped_db):
    connection, _, case_id, _, _, c1, _ = mapped_db
    cursor = connection.cursor(dictionary=True)
    cursor.execute("SELECT entity_name, claim_fingerprint FROM disclosure_claims WHERE claim_id = %s", (c1,))
    before = cursor.fetchone()
    connection.rollback()

    record_mapping(connection, case_id, **mapped())

    cursor.execute("SELECT entity_name, claim_fingerprint FROM disclosure_claims WHERE claim_id = %s", (c1,))
    assert cursor.fetchone() == before
    connection.rollback()
    cursor.close()
    (row,) = fetch_mappings(connection, case_id)
    connection.rollback()
    assert (row["subject_text"], row["status"], row["lei"]) == (SUBJECT, "MAPPED", LEI_A)


@pytest.mark.parametrize("status, columns", [
    ("MAPPED", {}),                                                    # no LEI
    ("MAPPED", {"lei": LEI_A, "name_type": "LEGAL_NAME"}),             # no evidence
    ("AMBIGUOUS", {"candidate_leis": f'["{LEI_A}"]'}),                  # one candidate
    ("AMBIGUOUS", {"lei": LEI_A, "candidate_leis": f'["{LEI_A}", "{LEI_B}"]'}),
    ("NO_LEI", {"lei": LEI_A}),                                         # invents an LEI
    ("UNRESOLVED", {"rationale": "  "}),
])
def test_database_status_and_evidence_checks(mapped_db, status, columns):
    connection, _, case_id, *_ = mapped_db

    with pytest.raises(errors.DatabaseError) as raised:
        raw(connection, case_id, status=status, **columns)
    assert raised.value.errno == 3819


def test_foreign_keys_and_case_isolation(mapped_db):
    connection, _, case_id, a, _, _, extra = mapped_db
    other_case, other_source = other_investigation(connection, extra, SUBJECT)
    unknown = synthetic_lei("SYNTHNONE")

    for columns in ({"status": "MAPPED", "lei": unknown, "name_type": "LEGAL_NAME",
                     "evidence_reference": "x", "evidence_location": "y", "limitations": "z"},
                    {"evidence_source_id": other_source}):
        with pytest.raises(errors.DatabaseError) as raised:
            raw(connection, case_id, **columns)
        assert raised.value.errno == 1452

    # Each case keeps its own identity history for the same text.
    record_mapping(connection, case_id, **mapped())
    record_mapping(connection, other_case, **mapped(lei=LEI_B, rationale="Synthetic other view"))
    mine, theirs = fetch_mappings(connection, case_id), fetch_mappings(connection, other_case)
    connection.rollback()
    assert [m["lei"] for m in mine] == [LEI_A] and [m["lei"] for m in theirs] == [LEI_B]
    assert all(m["case_id"] == case_id for m in mine)
    with pytest.raises(MappingError, match="not part of this investigation"):
        record_mapping(connection, case_id, **mapped(
            evidence_source_id=other_source, supersedes_mapping_id=mine[0]["mapping_id"]))


def test_exact_text_roots_and_same_subject_supersession(mapped_db):
    connection, _, case_id, *_ = mapped_db
    root = raw(connection, case_id)

    with pytest.raises(errors.DatabaseError) as raised:
        raw(connection, case_id)
    assert raised.value.errno == 1062

    # Exact text: case, trailing space and Unicode form make other subjects...
    for variant in (SUBJECT.upper(), SUBJECT + " ", "Fictional Harbour Holdings plć"):
        raw(connection, case_id, subject=variant)
        # ...which cannot supersede this one.
        with pytest.raises(errors.DatabaseError) as raised:
            raw(connection, case_id, subject=variant, supersedes=root)
        assert raised.value.errno == 1644

    raw(connection, case_id, supersedes=root)
    with pytest.raises(errors.DatabaseError) as raised:
        raw(connection, case_id, supersedes=root)
    assert raised.value.errno == 1062


def test_application_requires_exact_subject_from_the_claims(mapped_db):
    connection, _, case_id, *_ = mapped_db
    for variant in (SUBJECT.upper(), SUBJECT + " ", "Fictional Harbour Holdings"):
        with pytest.raises(MappingError, match="uses exactly that subject"):
            record_mapping(connection, case_id, **mapped(subject_text=variant))


def test_append_only(mapped_db):
    connection, _, case_id, *_ = mapped_db
    mapping_id = raw(connection, case_id)
    cursor = connection.cursor()
    try:
        for statement in ("UPDATE claim_subject_mappings SET rationale = 'x' WHERE mapping_id = %s",
                          "DELETE FROM claim_subject_mappings WHERE mapping_id = %s"):
            with pytest.raises(errors.DatabaseError) as raised:
                cursor.execute(statement, (mapping_id,))
            assert raised.value.errno == 1644
            connection.rollback()
    finally:
        cursor.close()


def test_reassignment_idempotency_and_concurrency(mapped_db):
    connection, _, case_id, *_ = mapped_db
    first = record_mapping(connection, case_id, **mapped())
    assert record_mapping(connection, case_id, **mapped()) == {**first, "created": False}

    barrier, outcomes = threading.Barrier(2), []

    def attempt(lei):
        own = connect()
        try:
            barrier.wait()
            outcomes.append(record_mapping(own, case_id, **mapped(
                lei=lei, rationale=f"Synthetic reassessment {lei}",
                supersedes_mapping_id=first["mapping_id"])))
        except MappingError as error:
            outcomes.append(error)
        finally:
            own.close()

    threads = [threading.Thread(target=attempt, args=(lei,)) for lei in (LEI_A, LEI_B)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sum(isinstance(o, dict) for o in outcomes) == 1
    assert sum(isinstance(o, MappingError) for o in outcomes) == 1


def test_ambiguous_never_becomes_a_single_identity(mapped_db):
    connection, _, case_id, *_ = mapped_db
    record_mapping(connection, case_id, **mapped(
        status="AMBIGUOUS", lei=None, name_type=None, candidate_leis=[LEI_A, LEI_B]))
    (row,) = fetch_mappings(connection, case_id)
    connection.rollback()

    assert row["status"] == "AMBIGUOUS" and row["lei"] is None


def test_mappings_do_not_change_snapshot_versions_1_to_3(mapped_db):
    connection, _, case_id, *_ = mapped_db
    before = snapshot_now(case_id)

    record_mapping(connection, case_id, **mapped())
    after = snapshot_now(case_id)

    for version in ("1", "2", "3"):
        assert snapshot_sha256(as_version(before, version)) == snapshot_sha256(as_version(after, version))
    assert "claim_subject" not in str(after)
