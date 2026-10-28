
"""
Verify a directory of versioned report runs before it is backed up.

    python -m src.verify_run_backup <runs-root> [--covers <other-root>]

Every entry must be case_<id>/<run_id>/ holding exactly manifest.json
and report.md, with no symbolic links or unfinished (.tmp-*) runs, and
every run must pass prepare_run: manifest validation plus report,
snapshot and analysis hashes. With --covers, every run under the other
root must also be present in <runs-root> with byte-identical files, so
a backup copy cannot silently omit runs.

Read-only. Prints only counts and problem descriptions, never report
contents. Exit 0 if everything verifies, 1 otherwise.

Finder metadata files named exactly .DS_Store (regular files, not
links) are ignored at every level; they are never backed up. Every
other unexpected entry, hidden or not, is still a problem.
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if not __package__:
    sys.path.insert(0, str(ROOT))

from src.generate_versioned_report import (  # noqa: E402
    MANIFEST_FILE,
    REPORT_FILE,
    RUN_ID_PATTERN,
)
from src.report_registry import RegistryError, prepare_run  # noqa: E402

CASE_DIR_PATTERN = re.compile(r"^case_([1-9][0-9]*)$")
RUN_FILES = {MANIFEST_FILE, REPORT_FILE}
FINDER_METADATA = ".DS_Store"


def _entries(directory):
    """Directory entries, without Finder metadata files."""

    return sorted(
        p for p in directory.iterdir()
        if not (p.name == FINDER_METADATA and p.is_file() and not p.is_symlink())
    )


def list_runs(runs_root):
    """Return ([(case_id, run_id)], problems) for a runs root."""

    runs, problems = [], []
    root = Path(runs_root)

    if root.is_symlink() or not root.is_dir():
        return runs, [f"{root.name} is not a directory"]

    for case_dir in _entries(root):
        match = CASE_DIR_PATTERN.match(case_dir.name)
        if case_dir.is_symlink() or not case_dir.is_dir() or not match:
            problems.append(f"unexpected entry {case_dir.name}")
            continue

        for run_dir in _entries(case_dir):
            label = f"{case_dir.name}/{run_dir.name}"
            if run_dir.is_symlink() or not run_dir.is_dir() \
                    or not RUN_ID_PATTERN.match(run_dir.name):
                problems.append(f"unexpected entry {label}")
                continue

            entries = _entries(run_dir)
            names = {p.name for p in entries}
            if names != RUN_FILES or any(p.is_symlink() for p in entries):
                problems.append(f"{label} does not hold exactly {sorted(RUN_FILES)}")
                continue

            runs.append((int(match.group(1)), run_dir.name))

    return runs, problems


def verify_runs(runs_root, covers=None):
    """Return (run_count, problems); the runs verified only if no problems."""

    runs, problems = list_runs(runs_root)

    for case_id, run_id in runs:
        try:
            prepare_run(case_id, run_id, runs_root)
        except (ValueError, RegistryError) as error:
            problems.append(f"case_{case_id}/{run_id}: {error}")

    if covers is not None:
        expected, other_problems = list_runs(covers)
        problems += [f"source: {p}" for p in other_problems]

        for case_id, run_id in expected:
            relative = Path(f"case_{case_id}") / run_id
            for name in sorted(RUN_FILES):
                copy = Path(runs_root) / relative / name
                if not copy.is_file() or copy.read_bytes() != \
                        (Path(covers) / relative / name).read_bytes():
                    problems.append(f"{relative}/{name} is missing or differs "
                                    "in the backup copy")

    return len(runs), problems


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m src.verify_run_backup")
    parser.add_argument("runs_root", type=Path)
    parser.add_argument("--covers", type=Path, default=None)
    args = parser.parse_args(argv)

    verified, problems = verify_runs(args.runs_root, args.covers)

    for problem in problems:
        print(f"Problem: {problem}", file=sys.stderr)

    if problems:
        print(f"Report runs: FAILED ({len(problems)} problem(s))", file=sys.stderr)
        return 1

    print(f"Report runs: {verified} run(s) verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
