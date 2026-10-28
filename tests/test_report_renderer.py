
import mysql.connector
import pytest

from src.evidence_snapshot import snapshot_sha256
from src.generate_versioned_report import render_run
from src.report_renderer import clean_url, format_value


CODE = {"git_commit": "abc123", "working_tree_dirty": False}


def render(snapshot):
    report, _, manifest = render_run(
        snapshot, 2024, 2025,
        "test-run", "2026-02-01T12:00:00Z", CODE,
    )
    return report.decode("utf-8"), manifest


def row_for(report, label):
    return next(
        line for line in report.splitlines()
        if line.startswith(f"| {label} |")
    )


def test_one_claim_per_metric(snapshot_tools):
    t = snapshot_tools
    report, manifest = render(t.snapshot([
        t.claim(1, 1, "49", year=2024),
        t.claim(2, 2, "49", year=2025),
        t.claim(3, 1, "Example Parent B.V.", year=2024,
                metric="management_controller", location="Note 2"),
        t.claim(4, 2, "Example Parent B.V.", year=2025,
                metric="management_controller", location="Note 3"),
    ]))

    row = row_for(report, "Equity Interest Pct")
    assert "| 2024-09-30 | 49% | 2025-09-30 | 49% | 0 | UNCHANGED |" in row
    assert "[S1·C1: Page 1], [S2·C2: Page 1]" in row

    assert "No disclosure in this snapshot is independently corroborated" in report
    assert "4 of 4 disclosure(s) rest on a single recorded claim" in report
    assert {r["comparison_status"] for r in manifest["analysis"]["comparison"]} == {
        "UNCHANGED"
    }


def test_corroborating_sources_keep_own_locations(snapshot_tools):
    t = snapshot_tools
    report, _ = render(t.snapshot([
        t.claim(10, 1, "49", year=2024),
        t.claim(11, 2, "49", year=2025, location="Page 3"),
        t.claim(12, 3, "49.0%", year=2025, location="Note 7"),
    ]))

    row = row_for(report, "Equity Interest Pct")
    assert "[S1·C10: Page 1], [S2·C11: Page 3], [S3·C12: Note 7]" in row
    assert "| CONSISTENT_SOURCES | S2, S3 | C11, C12 |" in report
    assert "1 disclosure(s) are reported consistently" in report


def test_conflict_has_no_selected_value(snapshot_tools):
    t = snapshot_tools
    report, _ = render(t.snapshot([
        t.claim(20, 1, "49", year=2024),
        t.claim(21, 2, "49", year=2025, location="Page 3"),
        t.claim(22, 3, "49%", year=2025, location="Page 5"),
        t.claim(23, 4, "60", year=2025, location="Page 9"),
    ]))

    row = row_for(report, "Equity Interest Pct")
    assert "Unresolved (see D)" in row
    assert "| — | UNRESOLVED_CONFLICT |" in row
    assert "60%" not in row.split("| UNRESOLVED_CONFLICT |")[0]

    assert "UNRESOLVED_CONFLICT (CROSS_SOURCE)" in report
    assert "- C21 from S2: 49% (Page 3)" in report
    assert "- C22 from S3: 49% (Page 5)" in report
    assert "- C23 from S4: 60% (Page 9)" in report
    assert "no value was selected" in report


def test_within_source_conflict(snapshot_tools):
    t = snapshot_tools
    report, _ = render(t.snapshot([
        t.claim(30, 1, "49", year=2025, location="Page 2"),
        t.claim(31, 1, "51", year=2025, location="Page 8"),
    ]))

    assert "UNRESOLVED_CONFLICT (WITHIN_SOURCE)" in report
    assert "- C30 from S1: 49% (Page 2)" in report
    assert "- C31 from S1: 51% (Page 8)" in report


def test_missing_period_renders_cleanly(snapshot_tools):
    t = snapshot_tools
    report, _ = render(t.snapshot([t.claim(40, 1, "49", year=2024)]))

    row = row_for(report, "Equity Interest Pct")
    assert "| Not recorded |" in row
    assert "| — | Not recorded | — | MISSING_PERIOD |" in row
    assert "[S: ]" not in report
    assert "| % |" not in report
    assert "[S1·C40: Page 1]" in row


def test_incompatible_periods_flagged(snapshot_tools):
    t = snapshot_tools
    report, _ = render(t.snapshot([
        t.claim(50, 1, "49", year=2024, period_end="2024-09-30"),
        t.claim(51, 2, "49", year=2025, period_end="2025-12-31"),
    ]))

    row = row_for(report, "Equity Interest Pct")
    assert "| 2024-09-30 | 49% | 2025-12-31 | 49% | — | PERIOD_MISMATCH |" in row
    assert "| PERIOD_MISMATCH | PERIOD_MISMATCH | FISCAL_YEAR_END_DIFFERS |" in report
    assert "more than one fiscal year-end (09-30, 12-31)" in report


def test_multiple_periods_in_year_not_chosen(snapshot_tools):
    t = snapshot_tools
    report, _ = render(t.snapshot([
        t.claim(60, 1, "49", year=2024),
        t.claim(61, 2, "49", year=2025, period_end="2025-09-30"),
        t.claim(62, 3, "50", year=2025, period_end="2025-12-31"),
    ]))

    row = row_for(report, "Equity Interest Pct")
    assert "2025-09-30, 2025-12-31 | Not determined (see E)" in row
    assert "MULTIPLE_PERIODS_IN_YEAR" in report


@pytest.mark.parametrize(
    "metric,value,expected",
    [
        ("equity_pct", "49", "49%"),
        ("equity_pct", "49%", "49%"),
        ("equity_pct", " 49.5% ", "49.5%"),
        ("equity_pct", "", ""),
        ("controller", "Company A", "Company A"),
    ],
)
def test_format_value(metric, value, expected):
    assert format_value(metric, value) == expected


def test_percent_suffix_not_doubled(snapshot_tools):
    t = snapshot_tools
    report, _ = render(t.snapshot([
        t.claim(70, 1, "49%", year=2024),
        t.claim(71, 2, "49%", year=2025),
    ]))

    assert "%%" not in report
    assert "| 49% |" in row_for(report, "Equity Interest Pct")


def test_source_assessment_change_is_rendered_and_hashed(snapshot_tools):
    t = snapshot_tools
    claims = [t.claim(80, 1, "49", year=2024), t.claim(81, 1, "49", year=2025)]

    reviewed = t.snapshot(claims)
    history = reviewed["sources"][0]["assessment_history"] + [{
        "history_id": 99,
        "event_type": "REVIEW",
        "previous_assessment": "SUPPORTS",
        "new_assessment": "INCONCLUSIVE",
        "review_reason": "Fictional re-review",
        "recorded_at_utc": "2026-01-05T00:00:00.000000Z",
        "annotations": [],
    }]
    changed = t.snapshot(
        claims, sources=[t.source(1, "INCONCLUSIVE", history=history)]
    )

    assert snapshot_sha256(reviewed) != snapshot_sha256(changed)

    report, _ = render(changed)
    assert "Source-level assessment: INCONCLUSIVE (history #99" in report


def test_corrections_documented_not_competing(snapshot_tools):
    t = snapshot_tools
    correction = {
        "correction_id": 5,
        "original_claim_id": 90,
        "replacement_claim_id": 92,
        "correction_reason": "Fictional transcription error",
        "corrected_at_utc": "2026-01-03T00:00:00.000000Z",
        "source_id": 2,
        "metric": "equity_interest_pct",
        "report_year": 2025,
        "period_end": "2025-09-30",
        "original_value": "94",
        "original_location": "Page 4",
        "original_fingerprint": "a" * 64,
        "replacement_value": "49",
        "replacement_location": "Page 4",
        "replacement_fingerprint": "b" * 64,
    }

    report, _ = render(t.snapshot(
        [t.claim(91, 1, "49", year=2024),
         t.claim(92, 2, "49", year=2025, location="Page 4")],
        corrections=[correction],
    ))

    assert (
        "| #5 | C90: 94% (Page 4) | C92: 49% (Page 4) "
        "| Fictional transcription error |" in report
    )
    # The superseded value is not a competing claim.
    assert "UNRESOLVED_CONFLICT" not in row_for(report, "Equity Interest Pct")
    assert "| SINGLE_CLAIM | S2 | C92 |" in report


def test_registry_cleans_urls_and_handles_missing_metadata(snapshot_tools):
    t = snapshot_tools
    source = t.source(1)
    source["publisher"] = None
    unused = t.source(9)

    report, manifest = render(t.snapshot(
        [t.claim(100, 1, "49", year=2025)], sources=[source, unused]
    ))

    assert "(<https://example.org/report-1.pdf?download=1#page=4>)" in report
    assert "utm_source" not in report
    assert "Publisher: not recorded; publication date: not recorded" in report
    assert "Claims in this report: none" in report
    # The stored URL is preserved in the manifest.
    assert "utm_source=newsletter" in (
        manifest["evidence_snapshot"]["sources"][0]["source_url"]
    )


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://a.example/x.pdf?utm_source=chatgpt.com",
         "https://a.example/x.pdf"),
        ("https://a.example/x.pdf?utm_source=c&id=5&UTM_Medium=m#page=18",
         "https://a.example/x.pdf?id=5#page=18"),
        ("https://a.example/x?token%20a=1+2&gclid=z",
         "https://a.example/x?token%20a=1+2"),
        ("https://a.example/x.html#section", "https://a.example/x.html#section"),
        ("", ""),
        (None, None),
    ],
)
def test_clean_url(url, expected):
    assert clean_url(url) == expected


def test_markdown_table_injection_escaped(snapshot_tools):
    t = snapshot_tools
    report, _ = render(t.snapshot([
        t.claim(110, 1, "49", year=2025, location="Page 1 | injected\ncell"),
    ]))

    row = row_for(report, "Equity Interest Pct")
    assert "Page 1 \\| injected cell" in row


def test_rendering_is_deterministic_and_offline(snapshot_tools, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Database connection attempted")

    monkeypatch.setattr(mysql.connector, "connect", fail)

    t = snapshot_tools
    snapshot = t.snapshot([
        t.claim(120, 1, "49", year=2024),
        t.claim(121, 2, "51", year=2025),
    ])

    first = render_run(snapshot, 2024, 2025, "r1", "2026-02-01T12:00:00Z", CODE)
    second = render_run(snapshot, 2024, 2025, "r1", "2026-02-01T12:00:00Z", CODE)

    assert first[0] == second[0]
    assert first[1] == second[1]
