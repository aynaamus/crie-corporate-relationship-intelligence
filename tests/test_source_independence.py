
"""
Agreement is not corroboration: document identity, shared
publishers and recorded independence (Milestone 23A, A1).
"""

import pandas as pd
import pytest

from src.claim_reconciliation import (
    CONSISTENT_RELATED_SOURCES,
    CONSISTENT_SOURCES,
    DUPLICATE_DOCUMENT_CLAIMS,
    INDEPENDENCE_UNVERIFIED,
    reconcile_claims,
)
from src.source_identity import document_keys, publisher_key, url_key


def claim(claim_id, source_id, value="49", year=2025, metric="interest_pct"):
    return {
        "claim_id": str(claim_id), "report_year": str(year),
        "period_end": f"{year}-12-31", "source_id": str(source_id),
        "entity": "Fictional Holdings plc", "metric": metric,
        "value": value, "source_location": "Page 3",
    }


def source(source_id, url=None, publisher=None, title=None, date=None):
    return {"source_id": source_id, "source_url": url, "publisher": publisher,
            "document_title": title or f"Fictional document {source_id}",
            "publication_date": date}


def status(sources, *claims, **provenance):
    row = reconcile_claims(pd.DataFrame(claims), sources=sources, **provenance).iloc[0]
    return row["reconciliation_status"], bool(row["corroborated"])


# Document identity

@pytest.mark.parametrize("a, b", [
    ("https://example.org/r.pdf", "HTTP://EXAMPLE.ORG/r.pdf"),
    ("https://example.org/r.pdf#page=4", "https://example.org/r.pdf#page=9"),
    ("https://example.org/r.pdf?utm_source=mail&fbclid=1", "https://example.org/r.pdf"),
    ("https://example.org:443/r.pdf?a=1&b=2", "https://example.org/r.pdf?b=2&a=1"),
])
def test_same_document_urls(a, b):
    assert url_key(a) == url_key(b)


@pytest.mark.parametrize("a, b", [
    ("https://example.org/report?id=2024", "https://example.org/report?id=2025"),
    ("https://example.org/r.pdf", "https://example.org/R.pdf"),
    ("https://example.org/r.pdf", "https://example.com/r.pdf"),
    ("https://example.org/r.pdf?lang=sk", "https://example.org/r.pdf"),
])
def test_other_query_parameters_and_paths_keep_documents_distinct(a, b):
    assert url_key(a) != url_key(b)


def test_publisher_key_ignores_form_not_substance():
    assert publisher_key("SPP Infrastructure, a.s.") == publisher_key("spp infrastructure a. s.")
    assert publisher_key("Société Générale") == publisher_key("SOCIETE GENERALE")
    assert publisher_key("EP Infrastructure, a.s.") != publisher_key("SPP Infrastructure, a.s.")
    assert publisher_key(None) is None and publisher_key("  ") is None


def test_document_keys_merge_transitively_and_only_on_evidence():
    keys = document_keys([
        source(1, url="https://e.org/a"),
        source(2, publisher="P", title="Report", date="2025-01-01"),
        source(3, url="https://e.org/a#p2", publisher="p.", title="report", date="2025-01-01"),
        source(4, url="https://e.org/b"),
        source(5, publisher="P", title="Report"),  # no date: not enough
    ])

    assert keys[1] == keys[2] == keys[3]
    assert len({keys[1], keys[4], keys[5]}) == 3


# Reconciliation statuses

def test_duplicate_registration_of_one_document():
    sources = [source(1, url="https://e.org/r.pdf"),
               source(2, url="https://e.org/r.pdf?utm_campaign=x#page=2")]

    assert status(sources, claim(10, 1), claim(11, 2)) == (DUPLICATE_DOCUMENT_CLAIMS, False)


def test_republished_document_is_not_independent():
    # Same title republished by another publisher at another URL.
    sources = [source(1, url="https://issuer.example/r.pdf", publisher="Issuer plc", title="Annual Report"),
               source(2, url="https://mirror.example/r.pdf", publisher="Mirror Ltd", title="Annual Report")]

    assert status(sources, claim(10, 1), claim(11, 2)) == (INDEPENDENCE_UNVERIFIED, False)


def test_shared_publisher_is_related():
    sources = [source(1, url="https://e.org/2025.pdf", publisher="Issuer plc"),
               source(2, url="https://e.org/notes.pdf", publisher="ISSUER PLC.")]

    assert status(sources, claim(10, 1), claim(11, 2)) == (CONSISTENT_RELATED_SOURCES, False)


def test_recorded_dependence_is_related():
    sources = [source(1, url="https://a.example/r.pdf", publisher="Parent plc"),
               source(2, url="https://b.example/r.pdf", publisher="Subsidiary a.s.")]

    result = status(sources, claim(10, 1), claim(11, 2), related_source_pairs=[(1, 2)])

    assert result == (CONSISTENT_RELATED_SOURCES, False)


def test_different_publishers_alone_do_not_establish_independence():
    sources = [source(1, url="https://a.example/r.pdf", publisher="Regulator"),
               source(2, url="https://b.example/r.pdf", publisher="Newspaper")]

    assert status(sources, claim(10, 1), claim(11, 2)) == (INDEPENDENCE_UNVERIFIED, False)


def test_unknown_publishers_and_missing_metadata_stay_unverified():
    assert status([source(1), source(2)], claim(10, 1), claim(11, 2)) == (INDEPENDENCE_UNVERIFIED, False)
    # No source metadata at all.
    assert status(None, claim(10, 1), claim(11, 2)) == (INDEPENDENCE_UNVERIFIED, False)


def test_recorded_independence_establishes_corroboration():
    sources = [source(1, url="https://a.example/r.pdf", publisher="Regulator"),
               source(2, url="https://b.example/r.pdf", publisher="Auditor")]

    result = status(sources, claim(10, 1), claim(11, 2), independent_source_pairs=[(2, 1)])

    assert result == (CONSISTENT_SOURCES, True)


@pytest.mark.parametrize("sources", [
    [source(1, url="https://a.example/r.pdf", publisher="Issuer plc"),
     source(2, url="https://b.example/r.pdf", publisher="Issuer plc")],
    [source(1, url="https://a.example/r.pdf"), source(2, url="https://a.example/r.pdf")],
])
def test_recorded_independence_cannot_override_shared_publisher_or_same_document(sources):
    result = status(sources, claim(10, 1), claim(11, 2), independent_source_pairs=[(1, 2)])

    assert result[1] is False
    assert result[0] in (CONSISTENT_RELATED_SOURCES, DUPLICATE_DOCUMENT_CLAIMS)


def test_conflicts_are_unaffected():
    sources = [source(1, url="https://e.org/r.pdf"), source(2, url="https://e.org/r.pdf")]

    row = reconcile_claims(pd.DataFrame([claim(10, 1, "49"), claim(11, 2, "51")]),
                           sources=sources).iloc[0]

    assert row["reconciliation_status"] == "UNRESOLVED_CONFLICT"
    assert not row["corroborated"]


def test_legacy_rule_only_for_historical_version_1():
    sources = [source(1, url="https://e.org/r.pdf"), source(2, url="https://e.org/r.pdf")]

    assert status(sources, claim(10, 1), claim(11, 2), legacy=True) == (CONSISTENT_SOURCES, True)


def test_spp_shape_remains_single_claims():
    # One claim per metric and year, as in the SPP/EPI case.
    sources = [source(1, publisher="SPP Infrastructure, a.s."),
               source(3, publisher="SPP Infrastructure, a.s.")]
    claims = [claim(1, 1, "51", 2024, "a"), claim(2, 3, "51", 2025, "a"),
              claim(3, 1, "49", 2024, "b"), claim(4, 3, "49", 2025, "b")]

    result = reconcile_claims(pd.DataFrame(claims), sources=sources)

    assert set(result["reconciliation_status"]) == {"SINGLE_CLAIM"}
    assert not result["corroborated"].any()


# Through the report pipeline

import datetime  # noqa: E402

from src.generate_versioned_report import render_run  # noqa: E402


def as_v2(snapshot):
    v2 = dict(snapshot, snapshot_schema_version="2", historical_claims=[],
              lineage_schema="INSTALLED")
    v2["corrections"] = []
    return v2


@pytest.fixture
def agreeing(snapshot_tools):
    t = snapshot_tools
    # Two registered sources from the same (fictional) publisher agree.
    return t.snapshot([
        t.claim(11, 1, "49", year=2024),
        t.claim(12, 1, "49", year=2025),
        t.claim(13, 2, "49", year=2025, location="Note 4"),
    ])


def render(snapshot):
    report, manifest, _ = render_run(
        snapshot, 2024, 2025, "run-independence",
        "2026-02-01T12:00:00.000000Z", {"git_commit": "a" * 40, "working_tree_dirty": False},
    )
    return report.decode("utf-8"), manifest


def test_version_2_report_states_agreement_not_corroboration(agreeing):
    report, manifest = render(as_v2(agreeing))
    analysis = __import__("json").loads(manifest)["analysis"]

    (row,) = [r for r in analysis["reconciliation"] if r["report_year"] == "2025"]
    assert row["reconciliation_status"] == "CONSISTENT_RELATED_SOURCES"
    assert row["corroborated"] is False
    assert "| CONSISTENT_RELATED_SOURCES | S1, S2 | C12, C13 |" in report
    assert "No disclosure in this snapshot is independently corroborated" in report
    assert "this is agreement, not corroboration" in report
    assert "Different publishers alone do not establish independence" in report
    assert "A match is a strong indication, not proof of identity." in report


def test_version_1_report_keeps_historical_rule(agreeing):
    report, manifest = render(agreeing)
    analysis = __import__("json").loads(manifest)["analysis"]

    (row,) = [r for r in analysis["reconciliation"] if r["report_year"] == "2025"]
    assert row["reconciliation_status"] == "CONSISTENT_SOURCES"
    assert "**CONSISTENT_SOURCES**: two or more distinct source documents" in report
    assert "INDEPENDENCE_UNVERIFIED" not in report
