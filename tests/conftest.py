
import sys
from pathlib import Path

import pytest

APP = str(Path(__file__).resolve().parents[1] / "app")


@pytest.fixture(autouse=True)
def _restore_app_modules():
    """
    AppTest runs app scripts in this process, and test scripts replace app
    module functions with stubs. Undo that after every test: restore the
    attributes of app modules loaded before it, and drop those it loaded, so
    no stub reaches another test.
    """

    def app_modules():
        return {name: module for name, module in list(sys.modules.items())
                if str(getattr(module, "__file__", "") or "").startswith(APP)}

    before = app_modules()
    saved = {name: dict(vars(module)) for name, module in before.items()}
    yield
    for name, module in app_modules().items():
        if name not in before:
            del sys.modules[name]
            continue
        for attribute, value in saved[name].items():
            if getattr(module, attribute, None) is not value:
                setattr(module, attribute, value)


def snap_claim(claim_id, source_id, value, year=2025,
               period_end=None, metric="equity_interest_pct",
               entity="Northwind Holdings plc", location="Page 1"):
    """A fictional current claim in snapshot format."""

    from src.comparison_rules import normalize_claim

    return {
        "claim_id": claim_id,
        "claim_fingerprint": f"{claim_id:064x}",
        "source_id": source_id,
        "entity": entity,
        "metric": metric,
        "report_year": year,
        "period_end": period_end or f"{year}-09-30",
        "value": value,
        "normalized_value": normalize_claim(metric, value),
        "source_location": location,
        "created_at_utc": "2026-01-01T00:00:00.000000Z",
    }


def snap_source(source_id, assessment="SUPPORTS", history=None,
                url=None, title=None):
    """A fictional registered source in snapshot format."""

    history = history if history is not None else [{
        "history_id": source_id * 10,
        "event_type": "REVIEW",
        "previous_assessment": "NOT_REVIEWED",
        "new_assessment": assessment,
        "review_reason": "Fictional review",
        "recorded_at_utc": "2026-01-02T00:00:00.000000Z",
        "annotations": [],
    }]

    return {
        "source_id": source_id,
        "document_title": title or f"Fictional Annual Report {source_id}",
        "publisher": "Fictional Publisher plc",
        "source_url": url or (
            f"https://example.org/report-{source_id}.pdf"
            "?utm_source=newsletter&download=1#page=4"
        ),
        "publication_date": None,
        "added_at_utc": "2026-01-01T00:00:00.000000Z",
        "evidence_assessment": assessment,
        "latest_assessment_history_id": (
            history[-1]["history_id"] if history else None
        ),
        "assessment_history": history,
        "metadata_changes": [],
    }


def make_snapshot(claims, sources=None, corrections=(),
                  case_id=7, years=(2024, 2025)):
    source_ids = sorted({c["source_id"] for c in claims})

    return {
        "snapshot_schema_version": "1",
        "case_id": case_id,
        "years": list(years),
        "captured_at_utc": "2026-02-01T12:00:00.000000Z",
        "claims": list(claims),
        "sources": sources or [snap_source(s) for s in source_ids],
        "corrections": list(corrections),
    }


@pytest.fixture
def snapshot_tools():
    """Builders for fictional evidence snapshots."""

    class Tools:
        claim = staticmethod(snap_claim)
        source = staticmethod(snap_source)
        snapshot = staticmethod(make_snapshot)

    return Tools
