
"""Investigation ownership, drafts and publication, without a database."""

import re
import sys
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "scripts")]

import access  # noqa: E402
import case_management  # noqa: E402
import demo_synthetic_case as demo  # noqa: E402

ALICE = access.User("oidc:alice", "Alice")
BOB = access.User("oidc:bob", "Bob")
LOCAL = access.User("local:analyst", "analyst")


# Who may write

@pytest.mark.parametrize("owner, user, allowed", [
    ("oidc:alice", ALICE, True),
    ("oidc:alice", BOB, False),
    ("oidc:alice", None, False),
    ("", LOCAL, True),                         # a legacy case: the local investigator's
    ("", ALICE, False),
    (access.DEMO_OWNER, ALICE, False),         # the published example is nobody's to change
    (access.DEMO_OWNER, LOCAL, False),
])
def test_may_write(owner, user, allowed):
    assert access.may_write(owner, user) is allowed


def test_local_identity_never_applies_when_hosted(monkeypatch):
    monkeypatch.setenv(access.LOCAL_USER, "analyst")
    assert access.current_user() == LOCAL
    monkeypatch.setenv(access.HOSTED, "1")
    assert access.current_user() is None


def test_hosted_crie_serves_only_the_demonstration_database(monkeypatch):
    monkeypatch.setenv("MYSQL_DATABASE", "corporate_intelligence")
    access.require_hosted_target()                                   # local: unchanged
    monkeypatch.setenv(access.HOSTED, "1")
    with pytest.raises(access.HostingConfigurationError):
        access.require_hosted_target()
    monkeypatch.setenv("MYSQL_DATABASE", access.DEMO_DATABASE)
    access.require_hosted_target()


class Connection:
    """Answers the ownership-schema check and the owner lookup."""

    def __init__(self, installed=True, owner="oidc:alice"):
        self.installed, self.owner, self.last = installed, owner, None

    def cursor(self, **kwargs):
        return self

    def execute(self, sql, params=None):
        self.last = sql

    def fetchone(self):
        if "information_schema" in self.last:
            return (1 if self.installed else 0,)
        return None if self.owner is None else (self.owner,)

    def close(self):
        pass

    def rollback(self):
        pass


def test_require_write(monkeypatch):
    access.require_write(Connection(), 7, ALICE)
    with pytest.raises(access.AccessDenied):
        access.require_write(Connection(), 7, BOB)
    with pytest.raises(access.AccessDenied):
        access.require_write(Connection(owner=None), 7, ALICE)        # no such case
    access.require_write(Connection(installed=False), 7, None)       # single-user installation
    monkeypatch.setenv(access.HOSTED, "1")
    with pytest.raises(access.AccessDenied):
        access.require_write(Connection(installed=False), 7, ALICE)


def test_cases_are_filtered_to_their_owner(monkeypatch):
    monkeypatch.setattr(access, "current_user", lambda: None)
    assert case_management.owner_condition(Connection()) == (" AND FALSE", ())
    assert case_management.owner_condition(Connection(), ALICE) == (
        " AND owner_subject = %s", ("oidc:alice",))
    assert case_management.owner_condition(Connection(), LOCAL) == (
        " AND owner_subject IN (%s, '')", ("local:analyst",))
    assert case_management.owner_condition(Connection(installed=False)) == ("", ())


def test_a_refused_write_never_reaches_the_recording_function(monkeypatch):
    import claim_subject_ui
    import source_relationships_ui

    called = []
    monkeypatch.setattr(access, "current_user", lambda: BOB)
    monkeypatch.setattr(claim_subject_ui, "record_mapping", lambda *a, **k: called.append(1))
    monkeypatch.setattr(source_relationships_ui, "record_assessment",
                        lambda *a, **k: called.append(1))
    for module in (claim_subject_ui, source_relationships_ui):
        with pytest.raises(access.AccessDenied):
            module._save(Connection, 7, {})
    assert called == []


def test_every_write_path_checks_ownership():
    for path in sorted((ROOT / "app").glob("*.py")):
        code = path.read_text()
        writes = re.findall(r"\.commit\(\)|record_mapping\(conn|record_assessment\(conn|"
                            r"action\(connection=|register_run\(", code)
        if writes and path.name not in ("access.py",):
            assert "access.require_write(" in code, path.name


# Pages

MINE = """
import sys
import pandas as pd
sys.path[:0] = [{root!r}, {root!r} + "/app"]
import streamlit as st
import investigations
st.session_state["crie_ownership"] = True
investigations.render_investigations(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), None, None,
                                     lambda: None)
"""


def test_signed_out_visitors_are_invited_to_sign_in(monkeypatch):
    monkeypatch.delenv(access.LOCAL_USER, raising=False)
    at = AppTest.from_string(MINE.format(root=str(ROOT)), default_timeout=30).run()
    assert not at.exception
    assert "Sign in to create investigations" in at.info[0].value
    assert not at.dataframe


PUBLISHED = """
import sys
import pandas as pd
sys.path[:0] = [{root!r}, {root!r} + "/app"]
import streamlit as st
import published
CALLS = st.session_state.setdefault("calls", [])
def row(i, title, owner, author):
    return {{"publication_id": i, "case_id": i * 10, "run_id": f"run{{i}}", "title": title,
            "summary": f"Summary {{i}}", "author_name": author, "owner_subject": owner,
            "published_at_utc": pd.Timestamp("2026-10-01"), "child_lei": "A" * 20,
            "parent_lei": "B" * 20, "relationship_type": "IS_DIRECTLY_CONSOLIDATED_BY",
            "snapshot_at_utc": pd.Timestamp("2026-06-01"), "child_name": "Synthetic A",
            "parent_name": "Synthetic B", "earlier": i - 1}}
published.load_published = lambda connect_db: pd.DataFrame([
    row(1, "Synthetic example", "crie:demonstration", "CRIE demonstration"),
    row(2, "Alice's investigation", "oidc:alice", "Alice")])
published.published_run = lambda connect_db, case_id, run_id: {{"run_id": run_id,
                                                               "report_sha256": "x"}}
published.show_verified_report = lambda case_id, run: CALLS.append(("report", case_id,
                                                                    run["run_id"]))
published.render_relationship_history = lambda case, connect_db: CALLS.append(("history",))
published._verify_all = lambda case_id, runs, connect_db, reproducibility: {{
    runs[0]["run_id"]: {{"integrity": {{"status": "INTACT"}},
                         "currency": {{"status": "CURRENT"}}}}}}
published.render_published_page(lambda: None)
"""


def test_published_investigations_list_open_and_verify():
    at = AppTest.from_string(PUBLISHED.format(root=str(ROOT)), default_timeout=30).run()

    assert not at.exception
    assert at.caption[1].value.startswith("Featured example (synthetic data)")
    assert [b.label for b in at.button] == ["Read this investigation"] * 2

    at.button(key="read_publication_2").click().run()
    text = " ".join(m.value for m in at.markdown)
    assert "Alice's investigation" in text and "Summary 2" in text
    assert "1 earlier publication(s) remain recorded" in " ".join(c.value for c in at.caption)
    assert at.session_state["calls"] == [("report", 20, "run2"), ("history",)]

    at.button(key="verify_publication_2").click().run()
    table = at.dataframe[0].value
    assert (table["Integrity"].tolist(), table["Evidence currency"].tolist()) == (
        ["INTACT"], ["CURRENT"])


PUBLISHING = """
import sys
sys.path[:0] = [{root!r}, {root!r} + "/app"]
import streamlit as st
import publishing
st.session_state["crie_can_write"] = {can_write!r}
publishing.case_publications = lambda connect_db, case_id: []
publishing.claim_years = lambda connect_db, case_id: [2024, 2025]
publishing.list_registered_runs = lambda connection, case_id: [{{
    "run_id": "r1", "generated_at_utc": "2026-10-01 09:00:00"}}]
class C:
    def rollback(self): pass
    def close(self): pass
publishing.render_publishing(7, C)
"""


@pytest.mark.parametrize("can_write", [True, False])
def test_publishing_controls_follow_ownership(can_write):
    at = AppTest.from_string(PUBLISHING.format(root=str(ROOT), can_write=can_write),
                             default_timeout=30).run()
    assert not at.exception
    assert "Draft: only you can see this investigation" in at.info[0].value
    controls = {b.label: b.disabled for b in at.button}
    assert controls == {"Create report version": not can_write, "Publish": not can_write}


# Schema

def test_migration_adds_only_ownership_and_publication():
    sql = (ROOT / "sql/e2e/m24_e2e_investigation_ownership.sql").read_text()
    code = re.sub(r"--[^\n]*", "", sql)
    assert "corporate_intelligence." not in code and "corporate_intelligence_test" not in code
    assert re.findall(r"ALTER TABLE (\S+)", code) == [
        "corporate_intelligence_e2e.investigation_cases"]
    assert "ADD COLUMN owner_subject" in code and "DROP INDEX uq_investigation;" in code
    assert ("ADD UNIQUE KEY uq_investigation_owner (\n"
            "        child_lei, parent_lei, relationship_type, snapshot_at_utc, owner_subject)"
            in code)
    assert re.findall(r"CREATE TABLE (\S+)", code) == [
        "corporate_intelligence_e2e.investigation_publications"]
    assert sorted(re.findall(r"CREATE TRIGGER \S+\.(\S+)", code)) == [
        "trg_investigation_cases_owner_fixed", "trg_publications_insert",
        "trg_publications_no_delete", "trg_publications_no_update"]
    assert "DELETE" not in re.sub(r"BEFORE DELETE|trg_publications_no_delete", "", code)


def test_schema_bundle_includes_ownership_and_only_drops_the_view_definer():
    bundle = demo.schema_bundle()
    positions = [bundle.index(f"-- ===== {n} =====") for n in demo.SCHEMA_SCRIPTS]
    assert positions == sorted(positions)
    assert demo.SCHEMA_SCRIPTS[-1] == "m24_e2e_investigation_ownership.sql"
    code = re.sub(r"--[^\n]*", "", bundle)
    assert "DEFINER" not in code
    assert "CREATE ALGORITHM=UNDEFINED */\n/*!50001 VIEW `v_current_disclosure_claims`" in bundle


# Guest sandboxes

SANDBOX = """
import sys
sys.path[:0] = [{root!r}, {root!r} + "/app"]
import streamlit as st
import access
st.session_state["crie_ownership"] = {ownership!r}
if st.button("start", key="start"):
    access.start_guest()
user = access.current_user()
st.session_state["user"] = None if user is None else user.subject
st.session_state["guest"] = access.is_guest(user)
"""


def sandbox_app(monkeypatch, sandbox="1", ownership=True):
    monkeypatch.delenv(access.LOCAL_USER, raising=False)
    monkeypatch.setenv(access.SANDBOX, sandbox)
    return AppTest.from_string(SANDBOX.format(root=str(ROOT), ownership=ownership),
                               default_timeout=30).run()


def test_a_guest_exists_only_once_a_visitor_starts_a_sandbox(monkeypatch):
    at = sandbox_app(monkeypatch)
    assert at.session_state["user"] is None
    at.button(key="start").click().run()
    token = at.query_params["sandbox"]
    token = token[0] if isinstance(token, list) else token
    assert re.fullmatch(r"[0-9a-f]{32}", token)
    assert at.session_state["guest"] is True
    subject = at.session_state["user"]
    assert subject.startswith("guest:") and token not in subject      # only a hash is stored
    at.run()
    assert at.session_state["user"] == subject                        # the same sandbox


@pytest.mark.parametrize("sandbox, ownership", [("0", True), ("1", False)])
def test_no_guests_without_the_sandbox_or_owners(monkeypatch, sandbox, ownership):
    at = sandbox_app(monkeypatch, sandbox, ownership)
    at.button(key="start").click().run()
    assert at.session_state["user"] is None


def test_guests_cannot_publish(monkeypatch):
    import publishing

    monkeypatch.setattr(access, "current_user", lambda: access.User("guest:abc", "Guest"))
    with pytest.raises(access.AccessDenied, match="sandbox work stays private"):
        publishing.publish(lambda: None, 7, {"run_id": "r"}, "t", "s", "a")


# Claim entry

CLAIMS = """
import sys
sys.path[:0] = [{root!r}, {root!r} + "/app"]
import streamlit as st
import disclosure_panel as panel
st.session_state["crie_can_write"] = {can_write!r}
RECORDED = st.session_state.setdefault("recorded", [])
panel._case_sources = lambda connect_db, case_id: [{{"source_id": 4, "document_title": "Filing"}}]
panel.record_claim = lambda connect_db, case_id, *fields: RECORDED.append(fields) or 99
panel.render_claim_entry(7, lambda: None)
"""


def claims_app(can_write=True):
    return AppTest.from_string(CLAIMS.format(root=str(ROOT), can_write=can_write),
                               default_timeout=30).run()


def test_a_claim_is_recorded_with_every_field():
    import datetime

    at = claims_app()
    assert not at.exception
    at.button[0].click().run()
    assert "Every field is required." in at.error[0].value
    assert at.session_state["recorded"] == []

    at.text_input[0].input("Synthetic Harbour Holdings plc")
    at.text_input[1].input("parent_interest_pct")
    at.number_input[0].set_value(2025)
    at.text_input[2].input("31/12/2025")
    at.text_input[3].input("60")
    at.text_input[4].input("Section 4")
    at.button[0].click().run()
    assert "The period end must use YYYY-MM-DD." in at.error[0].value
    assert at.text_input[3].value == "60"                        # nothing typed is lost
    at.text_input[2].input("2025-12-31")
    at.button[0].click().run()
    assert at.session_state["recorded"] == [(4, "Synthetic Harbour Holdings plc",
                                             "parent_interest_pct", 2025,
                                             datetime.date(2025, 12, 31), "60", "Section 4")]
    assert "Claim #99 recorded." in at.success[0].value


def test_claim_entry_is_disabled_for_readers():
    at = claims_app(can_write=False)
    assert at.button[0].disabled


def test_recording_a_claim_checks_ownership_first(monkeypatch):
    import datetime

    import disclosure_panel

    monkeypatch.setattr(access, "current_user", lambda: BOB)
    with pytest.raises(access.AccessDenied):
        disclosure_panel.record_claim(Connection, 7, 4, "Entity", "metric", 2025,
                                      datetime.date(2025, 12, 31), "60", "Page 1")
