
import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402


SETUP = """
import sys
from unittest.mock import MagicMock
sys.path[:0] = [{root!r}, {root!r} + "/app"]

import report_history

MODE = {mode!r}

def connect_db():
    if MODE == "unreachable":
        raise ConnectionError("database down")
    return MagicMock()

report_history.registry_installed = lambda c: MODE != "not_installed"
report_history.list_registered_runs = lambda c, case_id: (
    [] if MODE == "empty" else [{{
        "run_id": "20260201T120000000000Z-abcd1234",
        "generated_at_utc": "2026-02-01 12:00:00",
        "previous_year": 2024, "current_year": 2025,
        "claim_count": 8, "source_count": 4,
    }}]
)
report_history.fetch_registration = lambda c, run_id: {{"run": {{"run_id": run_id}}}}
report_history.capture_current_snapshot = lambda connect, case, years: {{}}
CALLS = []

def fake_verify(stored, **kwargs):
    CALLS.append(kwargs)
    # The dashboard must never enable historical code execution.
    assert kwargs.get("allow_historical_code") is False
    return {{
    "run_id": stored["run"]["run_id"],
    "verified_at_utc": "2026-02-02T00:00:00Z",
    "integrity": {{"status": "INTACT", "problems": []}},
    "currency": {{"status": "STALE", "problems": ["evidence changed"],
                  "changes": {{"claims_added": [9]}}}},
    }}

report_history.verify_registered_run = fake_verify
report_history.render_report_history(1, connect_db)

"""


def app(mode):
    from pathlib import Path

    root = str(Path(__file__).resolve().parents[1])
    return AppTest.from_string(
        SETUP.format(root=root, mode=mode), default_timeout=30
    ).run()


def test_unreachable_database_degrades_gracefully():
    at = app("unreachable")

    assert not at.exception
    assert "could not be reached" in at.info[0].value


def test_registry_not_installed_degrades_gracefully():
    at = app("not_installed")

    assert not at.exception
    assert "registry is not installed" in at.info[0].value


def test_no_registered_runs():
    at = app("empty")

    assert not at.exception
    assert "No report runs are registered" in at.info[0].value


def test_runs_listed_unverified_until_requested():
    at = app("installed")

    assert not at.exception
    table = at.dataframe[0].value
    assert table["Run ID"].tolist() == ["20260201T120000000000Z-abcd1234"]
    assert table["Integrity"].tolist() == ["Not verified"]
    assert table["Evidence currency"].tolist() == ["Not verified"]


def test_verification_on_explicit_request():
    at = app("installed")
    at.button[0].click().run()

    assert not at.exception
    table = at.dataframe[0].value
    assert table["Integrity"].tolist() == ["INTACT"]
    assert table["Evidence currency"].tolist() == ["STALE"]
    assert table["Checked (UTC)"].tolist() == ["2026-02-02T00:00:00Z"]
    assert any("Details for" in e.label for e in at.expander)

    # No local filesystem paths are displayed.
    assert "/Users/" not in str(table.to_dict())


READER = """
import hashlib, sys
from unittest.mock import MagicMock
sys.path[:0] = [{root!r}, {root!r} + "/app"]
import report_history
REPORT = b"# Synthetic report\\n\\nRegistered text."
report_history.registry_installed = lambda c: True
report_history.list_registered_runs = lambda c, case_id: [{{
    "run_id": "r1", "generated_at_utc": "2026-02-01 12:00:00", "previous_year": 2024,
    "current_year": 2025, "claim_count": 1, "source_count": 1,
    "report_sha256": hashlib.sha256(REPORT).hexdigest()}}]
report_history.read_run = lambda case_id, run_id, runs_root=None: (b"{{}}", REPORT + {tamper!r})
report_history.render_report_history(1, lambda: MagicMock())
"""


def reader(tamper):
    from pathlib import Path

    root = str(Path(__file__).resolve().parents[1])
    at = AppTest.from_string(READER.format(root=root, tamper=tamper), default_timeout=30).run()
    at.selectbox(key="read_report_1").set_value("r1").run()
    return at


def test_a_registered_report_is_shown_only_when_it_matches_its_hash():
    at = reader(b"")
    assert not at.exception
    assert any("Registered text." in m.value for m in at.markdown)
    assert any("matches the registered hash" in c.value for c in at.caption)

    at = reader(b" altered")
    assert not at.exception
    assert "does not match its registered hash" in at.error[0].value
    assert not any("Registered text." in m.value for m in at.markdown)
