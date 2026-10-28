
"""
Register an existing versioned report run in the report registry.

    python -m src.register_report_run --case-id 1 \\
        --run-id <run_id> --database corporate_intelligence_test

Dry run by default: validates the run files and checks database
compatibility in a READ ONLY transaction, writing nothing. Add
--commit to register. Registration in production additionally
requires --confirm-production "REGISTER <run_id> IN corporate_intelligence",
because registrations are append-only and cannot be undone through
the application.

The database is always the one named by --database; there is no
fallback to MYSQL_DATABASE or any default. Report files are only
read, never created, regenerated, or modified.

Exit codes:
    0  dry run ready, registered, or already registered (identical)
    2  invalid command-line usage (argparse)
    3  registry tables are not installed
    4  conflicting registration exists for this run ID
    5  run does not match the database's evidence
    6  database target, connection, or identity problem
    7  missing or incorrect production confirmation
    8  run files or manifest failed validation
    1  unexpected error
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if not __package__:
    sys.path.insert(0, str(ROOT))

import mysql.connector  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from src.generate_versioned_report import DEFAULT_RUNS_DIR  # noqa: E402
from src.report_registry import (  # noqa: E402
    PREFLIGHT_ALREADY_REGISTERED,
    PREFLIGHT_CONFLICT,
    PREFLIGHT_EVIDENCE_MISMATCH,
    PREFLIGHT_NOT_INSTALLED,
    PREFLIGHT_READY,
    RegistrationConflict,
    RegistryError,
    RegistryUnavailable,
    preflight_registration,
    prepare_run,
    register_run,
)


PRODUCTION_DATABASE = "corporate_intelligence"
TEST_DATABASE = "corporate_intelligence_test"
ALLOWED_DATABASES = (TEST_DATABASE, PRODUCTION_DATABASE)

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_USAGE = 2
EXIT_NOT_INSTALLED = 3
EXIT_CONFLICT = 4
EXIT_EVIDENCE = 5
EXIT_DATABASE = 6
EXIT_CONFIRMATION = 7
EXIT_FILES = 8

PREFLIGHT_EXIT = {
    PREFLIGHT_READY: EXIT_OK,
    PREFLIGHT_ALREADY_REGISTERED: EXIT_OK,
    PREFLIGHT_NOT_INSTALLED: EXIT_NOT_INSTALLED,
    PREFLIGHT_CONFLICT: EXIT_CONFLICT,
    PREFLIGHT_EVIDENCE_MISMATCH: EXIT_EVIDENCE,
}


class DatabaseTargetError(Exception):
    """The requested database could not be safely used."""


def production_phrase(run_id):
    return f"REGISTER {run_id} IN {PRODUCTION_DATABASE}"


def _describe(error):
    """Error description without server messages (they may name accounts)."""

    errno = getattr(error, "errno", None)
    return type(error).__name__ + (f" (errno {errno})" if errno else "")


def connect_to(database):
    """
    Fresh connection to exactly the named database, with its
    identity verified. Credentials come from the environment
    (.env); the database name never does.
    """

    if database not in ALLOWED_DATABASES:
        raise DatabaseTargetError(f"Unexpected database target: {database}")

    load_dotenv(ROOT / ".env")

    try:
        connection = mysql.connector.connect(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_PORT", "3306")),
            user=os.getenv("MYSQL_USER"),
            password=os.getenv("MYSQL_PASSWORD"),
            database=database,
            time_zone="+00:00",
        )
    except mysql.connector.Error as error:
        raise DatabaseTargetError(
            f"Could not connect to {database}: {_describe(error)}"
        ) from None

    try:
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT DATABASE()")
            actual = cursor.fetchone()[0]
        finally:
            cursor.close()

        # End the implicit transaction opened by the identity query.
        connection.rollback()
    except Exception:
        connection.close()
        raise

    if actual != database:
        connection.close()
        raise DatabaseTargetError(
            f"Connected database is {actual!r}, expected {database!r}"
        )

    return connection


def _print(lines, stream):
    for line in lines:
        print(line, file=stream)


def run(argv=None, stdout=sys.stdout, stderr=sys.stderr):
    parser = argparse.ArgumentParser(
        prog="python -m src.register_report_run",
        description="Validate (dry run) or register an existing "
        "versioned report run.",
    )
    parser.add_argument("--case-id", type=int, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--database", required=True, choices=ALLOWED_DATABASES,
        help="Target database; no default and no fallback.",
    )
    parser.add_argument(
        "--runs-root", type=Path, default=DEFAULT_RUNS_DIR,
        help="Directory containing case_<id>/<run_id>/ run folders.",
    )
    parser.add_argument(
        "--commit", action="store_true",
        help="Register the run. Without it, nothing is written.",
    )
    parser.add_argument(
        "--confirm-production", default=None, metavar="PHRASE",
        help=f'Required with --commit on {PRODUCTION_DATABASE}: '
        f'"{production_phrase("<run_id>")}"',
    )
    args = parser.parse_args(argv)

    mode = "COMMIT" if args.commit else "DRY RUN (nothing will be written)"
    out = [f"Mode: {mode}", f"Run: case {args.case_id}, {args.run_id}"]

    if args.database == PRODUCTION_DATABASE and args.commit:
        if args.confirm_production != production_phrase(args.run_id):
            _print(out + [
                "Refused: registering in production is append-only and "
                "cannot be undone through the application.",
                "Re-run with --confirm-production "
                f'"{production_phrase(args.run_id)}" to proceed.',
            ], stderr)
            return EXIT_CONFIRMATION
    elif args.confirm_production is not None:
        _print(out + [
            "Refused: --confirm-production applies only to "
            f"--commit on {PRODUCTION_DATABASE}."
        ], stderr)
        return EXIT_CONFIRMATION

    runs_root = args.runs_root

    if runs_root.is_symlink() or not runs_root.is_dir():
        _print(out + ["File validation: FAILED (runs root is not a "
                      "directory or is a symbolic link)"], stderr)
        return EXIT_FILES

    # 1. Files only; no database access.
    try:
        registration = prepare_run(args.case_id, args.run_id, runs_root)
    except (ValueError, RegistryError) as error:
        _print(out + [f"File validation: FAILED ({error})"], stderr)
        return EXIT_FILES

    record = registration["run"]
    out += [
        "File validation: PASSED (report and manifest hashes verified)",
        f"  Evidence snapshot SHA-256: {record['evidence_snapshot_sha256']}",
        f"  Report SHA-256: {record['report_sha256']}",
        f"  Claims: {record['claim_count']}, sources: {record['source_count']}",
    ]

    # 2. Database compatibility on a fresh, verified connection.
    try:
        connection = connect_to(args.database)
    except DatabaseTargetError as error:
        _print(out + [f"Database: FAILED ({error})",
                      "Result: NOT VERIFIED against the database"], stderr)
        return EXIT_DATABASE

    try:
        out.append(f"Database: {args.database} (identity confirmed)")
        preflight = preflight_registration(connection, registration)
    except Exception as error:
        _print(out + [f"Database checks: FAILED ({_describe(error)})"], stderr)
        return EXIT_DATABASE
    finally:
        connection.close()

    status = preflight["status"]
    out.append(
        "Registry schema: "
        + ("installed" if preflight["registry_installed"] else "NOT INSTALLED")
    )

    if status == PREFLIGHT_EVIDENCE_MISMATCH:
        _print(out + [f"Evidence compatibility: FAILED ({p})"
                      for p in preflight["problems"]], stderr)
        return EXIT_EVIDENCE

    out.append("Evidence compatibility: CONFIRMED (claims, fingerprints, "
               "sources and assessment history match)")

    if status == PREFLIGHT_NOT_INSTALLED:
        _print(out + ["Result: NOT READY; files and evidence are valid, but "
                      "the registry is not installed"], stderr)
        return EXIT_NOT_INSTALLED

    if status == PREFLIGHT_CONFLICT:
        _print(out + [f"Result: CONFLICT ({p})" for p in preflight["problems"]],
               stderr)
        return EXIT_CONFLICT

    if status == PREFLIGHT_ALREADY_REGISTERED:
        _print(out + ["Result: ALREADY REGISTERED (identical); nothing to do"],
               stdout)
        return EXIT_OK

    if not args.commit:
        _print(out + ["Result: READY TO REGISTER (dry run; nothing was "
                      "written). Re-run with --commit to register."], stdout)
        return EXIT_OK

    # 3. Registration on its own fresh connection.
    try:
        connection = connect_to(args.database)
    except DatabaseTargetError as error:
        _print(out + [f"Registration: NOT ATTEMPTED ({error})"], stderr)
        return EXIT_DATABASE

    try:
        result = register_run(connection, args.case_id, args.run_id, runs_root)
    except RegistrationConflict as error:
        _print(out + [f"Result: CONFLICT ({error})"], stderr)
        return EXIT_CONFLICT
    except RegistryUnavailable:
        _print(out + ["Result: registry not installed; nothing written"], stderr)
        return EXIT_NOT_INSTALLED
    except RegistryError as error:
        _print(out + [f"Result: REJECTED ({error}); nothing written"], stderr)
        return EXIT_EVIDENCE
    except mysql.connector.Error as error:
        _print(out + [f"Result: FAILED ({_describe(error)}); the "
                      "transaction was rolled back"], stderr)
        return EXIT_DATABASE
    finally:
        connection.close()

    _print(out + [f"Result: {result['status']}"], stdout)
    return EXIT_OK


def main():
    try:
        code = run()
    except SystemExit:
        raise
    except Exception as error:
        print(f"Unexpected error: {_describe(error)}", file=sys.stderr)
        code = EXIT_UNEXPECTED
    sys.exit(code)


if __name__ == "__main__":
    main()
