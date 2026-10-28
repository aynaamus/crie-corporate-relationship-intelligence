
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from display import utc

from src.evidence_snapshot import sha256_hex
from src.report_registry import (
    DEFAULT_RUNS_DIR,
    fetch_registration,
    list_registered_runs,
    read_run,
    registry_installed,
)
from src.report_verification import (
    capture_current_snapshot,
    verify_registered_run,
)


NOT_VERIFIED_LABEL = "Not verified"


def runs_root():
    """Where run files are read from: CRIE_RUNS_ROOT (relative to the repository), else
    the registry's default, reports/runs."""

    configured = os.getenv("CRIE_RUNS_ROOT")
    if not configured:
        return DEFAULT_RUNS_DIR
    path = Path(configured)
    return path if path.is_absolute() else Path(__file__).resolve().parents[1] / path


def _load_runs(case_id, connect_db):
    """Return (installed, runs); raises on connection failure."""

    connection = connect_db()

    try:
        if not registry_installed(connection):
            return False, []
        return True, list_registered_runs(connection, case_id)
    finally:
        connection.rollback()
        connection.close()


def _verify_all(case_id, runs, connect_db, reproducibility):
    """Verify each registered run; evidence is read once per year pair."""

    current = {}
    # Version-4 evidence is read only when a version-4 run needs it;
    # earlier runs compare against its projection, which is identical.
    version = "4" if any(r.get("snapshot_schema_version") == "4" for r in runs) else None

    def load_current(case, years):
        if years not in current:
            try:
                current[years] = capture_current_snapshot(
                    connect_db, case, years, **({"version": version} if version else {})
                )
            except Exception as error:
                current[years] = error

        if isinstance(current[years], Exception):
            raise current[years]

        return current[years]

    results = {}
    connection = connect_db()

    try:
        for run in runs:
            stored = fetch_registration(connection, run["run_id"])

            if stored is None:
                continue

            # Historical code is never executed from the dashboard;
            # reproducibility here uses only the running code.
            results[run["run_id"]] = verify_registered_run(
                stored,
                load_current=load_current,
                reproducibility=reproducibility,
                allow_historical_code=False,
                runs_root=runs_root(),
            )
    finally:
        connection.rollback()
        connection.close()

    return results


def render_report_history(case_id, connect_db):
    """Registered report runs and their verification status."""

    st.divider()
    st.subheader("Report History", anchor="crie-reports")

    try:
        installed, runs = _load_runs(case_id, connect_db)
    except Exception:
        st.info(
            "Report history is unavailable because the database "
            "could not be reached. Other investigation tools are "
            "unaffected."
        )
        return

    if not installed:
        st.info(
            "The report registry is not installed in this database. "
            "Versioned reports can still be generated and verified "
            "from the command line."
        )
        return

    if not runs:
        st.info("No report runs are registered for this case.")
        return

    state_key = f"report_verification_{case_id}"
    results = st.session_state.setdefault(state_key, {})

    col1, col2 = st.columns([1, 2])
    reproducibility = col2.checkbox(
        "Include reproducibility (re-renders with the running code "
        "only; historical code is never executed here)",
        key=f"report_reproducibility_{case_id}",
    )

    if col1.button(
        "Check file integrity and evidence currency", key=f"verify_reports_{case_id}"
    ):
        try:
            with st.spinner("Checking report files and evidence..."):
                results.clear()
                results.update(
                    _verify_all(case_id, runs, connect_db, reproducibility)
                )
        except Exception as error:
            st.error(f"Verification could not complete: {type(error).__name__}")

    rows = []

    for run in runs:
        result = results.get(run["run_id"], {})

        rows.append({
            "Run ID": run["run_id"],
            "Generated": utc(run["generated_at_utc"]),
            "Years": f"{run['previous_year']}–{run['current_year']}",
            "Claims": run["claim_count"],
            "Sources": run["source_count"],
            "Integrity": result.get("integrity", {}).get(
                "status", NOT_VERIFIED_LABEL
            ),
            "Reproducibility": result.get("reproducibility", {}).get(
                "status", NOT_VERIFIED_LABEL
            ),
            "Evidence currency": result.get("currency", {}).get(
                "status", NOT_VERIFIED_LABEL
            ),
            "Checked (UTC)": result.get("verified_at_utc", "—"),
        })

    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    for run_id, result in results.items():
        details = {
            check: result[check]
            for check in ("integrity", "reproducibility", "currency")
            if check in result
            and (result[check]["problems"] or result[check].get("changes"))
        }

        if details:
            with st.expander(f"Details for {run_id}"):
                for check, outcome in details.items():
                    st.write(f"**{check.title()}: {outcome['status']}**")
                    for problem in outcome["problems"]:
                        st.write(f"- {problem}")
                    for kind, ids in outcome.get("changes", {}).items():
                        st.write(f"- {kind.replace('_', ' ')}: {ids}")

    _render_reader(case_id, runs)

    st.caption(
        "Integrity: saved files match their registered hashes (not proof "
        "that the disclosures are correct). Reproducibility: the saved "
        "evidence re-renders identically with the report's original code. "
        "Currency: CURRENT means the evidence is unchanged since the report; "
        "STALE means it has changed, not that the findings are false; "
        "UNKNOWN means no reliable comparison was possible."
    )


def _render_reader(case_id, runs):
    """Read a registered report; shown only when its file matches the registered hash."""

    with st.expander("Read a registered report"):
        ids = [run["run_id"] for run in runs]
        run_id = st.selectbox("Report run", ids, index=None, placeholder="Choose a run",
                              key=f"read_report_{case_id}")
        if run_id is None:
            return
        show_verified_report(case_id, next(r for r in runs if r["run_id"] == run_id))


def show_verified_report(case_id, run):
    """A registered run's report.md, shown only when it matches the registered hash."""

    run_id = run["run_id"]
    try:
        _, report = read_run(case_id, run_id, runs_root())
    except Exception as error:  # noqa: BLE001 - shown, never hidden
        st.error(f"The report files could not be read: {type(error).__name__}")
        return
    if sha256_hex(report) != run["report_sha256"]:
        st.error("The saved report does not match its registered hash, so it is not "
                 "shown. Check file integrity above.")
        return
    st.caption(f"report.md of {run_id}: its SHA-256 matches the registered hash.")
    st.markdown(report.decode("utf-8"))
