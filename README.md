# CRIE: Corporate Relationship Intelligence

Copyright (c) 2026 Ayna Musayeva. All rights reserved. No open-source licence is granted; see
[Copyright, data and dependencies](#7-copyright-data-and-dependencies).

CRIE is a local investigation workbench for reported corporate relationships. It starts from GLEIF
Level 2 data, in which legal entities report the parents that consolidate them in their accounts. It
lets an investigator open a case on one relationship, register the documents they find, transcribe
claims from those documents, and record how each source was reviewed. It then produces a versioned
report whose evidence and files can be checked later.

Every judgement in CRIE is recorded by the investigator, with its reasoning, and is kept: reviews,
corrections, source-independence assessments and identity mappings are append-only, so earlier
entries are superseded but never overwritten. CRIE does not decide whether a relationship is true,
who owns or controls a company, or whether anything is wrong. Section 5 describes what it does
and does not determine.

Contents: [1 What it does](#1-what-it-does) · [2 Setup](#2-setup) ·
[3 Demonstration](#3-demonstration) · [4 Architecture](#4-architecture) ·
[5 Evidence limitations](#5-evidence-limitations) · [6 Tests](#6-tests) ·
[7 Copyright, data and dependencies](#7-copyright-data-and-dependencies)

## 1. What it does

| Area | What CRIE records or shows |
|---|---|
| Relationships | GLEIF accounting-consolidation relationships (`IS_DIRECTLY_CONSOLIDATED_BY`, `IS_ULTIMATELY_CONSOLIDATED_BY`) as a network, with GLEIF's own validation classification and the file each row came from. |
| GLEIF history | Each GLEIF file kept as an observation: its SHA-256, its import batch, its coverage (full dataset, delta, sample, or full scan with quarantined records), and what it stated. Records are compared field by field (consistent, differs, single observation, not stated, not captured). |
| Quarantine | GLEIF records whose identifier fails the LEI check digits, or is provisional, are kept as evidence of what the file said. They never become trusted entities, relationships or mappings. |
| Cases | An investigation on one relationship, with status, notes and a manual analyst assessment. |
| Sources | Registered documents (title, publisher, URL, date), each with an evidence assessment (`NOT_REVIEWED`, `SUPPORTS`, `CONTRADICTS`, `INCONCLUSIVE`). Every review is kept with its reasoning. |
| Claims | Values transcribed from sources, with their location. A correction or restoration adds a new claim version; the original stays recorded. |
| Source independence | Recorded relationships between sources (for example `REPUBLICATION`, `SHARED_PUBLISHER`, `INDEPENDENT_ORIGIN`), which decide whether agreement between sources counts as corroboration. |
| Identity | Which legal entity a claim's subject text refers to: `MAPPED`, `AMBIGUOUS`, `NO_LEI` or `UNRESOLVED`, never matched automatically. |
| Reports | Snapshot-driven reports. Each run stores its exact evidence snapshot and hashes, and can later be checked for integrity (files unchanged), reproducibility (same output from the same code) and currency (evidence unchanged). |

The dashboard has four pages, and nothing is selected when it opens:
- **Investigate** (home): search for a company at the top. Its reported consolidation network
  appears right below, with the relationships to choose from and **Investigate this
  relationship**. Evidence, Timeline and **Research leads** sit below that. Research leads are
  the flagged-relationship worklist, either for the network or for the whole dataset. The chosen
  company and relationship carry across pages.
- **My Investigations:** your own investigations, drafts and published, listed by their
  permanent case IDs. An open case shows one section at a time: case and notes, relationship and
  GLEIF history, sources and assessments, claims with identities and corrections, and reports.
  The reports section is also where you create report versions and publish.
- **Published Investigations:** the investigations their authors chose to publish, with the
  synthetic example first. Each shows its author's title and summary, its published report
  (verified before it is shown) and the relationship's public GLEIF history. Drafts never appear
  here, and neither do merely flagged relationships.
- **Methodology:** CRIE's evidence rules, how investigations are published, the architecture,
  and the glossary.

**Owners, drafts and publication.** Once the ownership schema is installed
(`sql/e2e/m24_e2e_investigation_ownership.sql`, end-to-end database only):
- every investigation belongs to the user who opened it, and its owner never changes;
- drafts are visible only to their owner;
- every write checks ownership on the same connection before it writes;
- publishing appends a record that names one verified, registered report of the case. The
  database accepts it only from the case's owner, and earlier publications and reports are kept.

Visitors can try CRIE without an account in a **private sandbox** (`CRIE_SANDBOX=1`, on by
default when hosted):
- one click gives them a guest identity, kept in the page's link;
- their investigations are visible only to them, up to five per sandbox;
- guests cannot publish;
- `scripts/demo_synthetic_case.py cleanup-guests --older-than-hours N` removes old sandboxes.

Sign-in for publishing authors uses Streamlit's built-in authentication, which needs an `[auth]`
section in `.streamlit/secrets.toml`. The code path is implemented and unit-tested, but sign-in
through a real identity provider has not been verified. On your own machine, `CRIE_LOCAL_USER=<name>` gives a local single
user instead; it is ignored in a hosted installation. Without the ownership schema, as in the
working database today, CRIE stays single-user and every case is the local investigator's.

A case always stays on the GLEIF evidence row it was opened on. It remains available when a later
observation arrives or the relationship stops being listed as active. A user never opens a
second case on a relationship they already investigate, but other users can open their own.

Throughout, labels are investigator-facing; the stored values remain under "Technical values (as
stored)". Reviews annotated as accidental duplicates are kept in a collapsed audit record, not in
the main history tables. The glossary is also in the sidebar.

A warning banner marks any database other than the working one as demonstration data.

## 2. Setup

### Requirements

- Python 3.12. The versions in `requirements.txt` are the ones verified.
- A MySQL server, 8.0.18 or later. The full test suite was run on MySQL 26.7 locally. A complete
  deployment rehearsal (schema, accounts, example build, two sandboxes and an application restart)
  was run on MySQL 8.4.13, the version pinned for hosting. The application account needs
  `SELECT, INSERT, UPDATE, DELETE` on the CRIE databases.
- An administrator account for installing schemas. The application account never runs DDL.

```zsh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Configuration

Create `.env` in the repository root. Git ignores it.

```
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=<application account>
MYSQL_PASSWORD=<password>
MYSQL_DATABASE=<database the dashboard opens>
```

The dashboard always opens the database named in `.env` (it loads `.env` with override), whatever
the shell environment says. Command-line tools that take `--database` accept only the databases they
name, and never fall back to `MYSQL_DATABASE`.

### Databases

| Database | Purpose |
|---|---|
| `corporate_intelligence` | The working database. |
| `corporate_intelligence_test` | Integration tests. It has the evidence and audit tables but not the dashboard's legacy tables (`case_notes`, `relationship_periods`), so the dashboard does not run against it. |
| `corporate_intelligence_e2e` | A disposable copy of the working schema, used by the end-to-end tests and by the demonstration below. |

Schemas are installed by an administrator with the `mysql` client, one script at a time, without
`--force`. Each script states which database it is for. To build the end-to-end database, run these
in order:

```
sql/e2e/m23a_e2e_01_create.sql
sql/e2e/m23a_e2e_02_schema.sql
sql/e2e/m23a_e2e_03_registry.sql
sql/e2e/m23a_e2e_04_lineage.sql
sql/e2e/m23b1_e2e_source_assessments.sql
sql/e2e/m23b1_e2e_fix_supersession_trigger.sql
sql/e2e/m23b2a_e2e_claim_subject_mappings.sql
sql/e2e/m23b2b_e2e_gleif_observations.sql
sql/e2e/m23b2b3_e2e_gleif_quarantine.sql
sql/e2e/m24_e2e_investigation_ownership.sql
```

Then grant your own application account `SELECT, INSERT, UPDATE, DELETE` on
`corporate_intelligence_e2e`; no account-specific grant script is included. Migrations for the
working database are in `sql/migrations/`, and the test-database scripts are `sql/test_*.sql`.

### Launch

From the repository root:

```zsh
.venv/bin/python -m streamlit run app/dashboard.py
```

The dashboard also adds the repository root to the import path itself, so the plain `streamlit`
command works too, as hosting services run it.

**Hosted installation.** With `CRIE_HOSTED=1`:
- CRIE connects only to `corporate_intelligence_e2e`;
- it requires the ownership schema;
- it ignores `CRIE_LOCAL_USER`, so writing needs a signed-in user.

`scripts/demo_synthetic_case.py build` creates the synthetic example in one step, report included.
When owners are recorded, it also publishes the example, owned by a system owner no user can sign
in as.
Deploying to Railway (MySQL 8.4, a persistent volume for report files) and the files a public
repository should contain are covered in [docs/PUBLIC_DEMO.md](docs/PUBLIC_DEMO.md).

## 3. Demonstration

The demonstration uses only synthetic data, in the end-to-end database. It needs the end-to-end
database to be empty when you start, and you should empty it again afterwards (step 9), because the
end-to-end tests expect it to be empty.

All synthetic names begin with "Synthetic" and all LEIs with `SYNTHDEMO`. The data is:

- **Entities:** Synthetic Harbour Holdings plc, the reporting entity; Synthetic Parent Group AG,
  its direct consolidating parent; and Synthetic Ultimate Holdings SA, its ultimate parent.
- **Two GLEIF snapshots, June and October.** The October file reports a different accounting period
  and a different legal name for Harbour, and no longer lists the ultimate-parent relationship.
- **A quarantined record:** a fourth LEI with wrong check digits.

**1. Point the dashboard at the end-to-end database.** In `.env`, set
`MYSQL_DATABASE=corporate_intelligence_e2e`, and remember to restore it afterwards.

**2. Create and import the synthetic GLEIF files**, in this order:

```zsh
.venv/bin/python scripts/demo_synthetic_case.py files /tmp/crie-demo
for name in lei2-june rr-june lei2-october rr-october; do
  dataset=RR; [[ $name == lei2-* ]] && dataset=LEI2
  .venv/bin/python -m src.gleif_import /tmp/crie-demo/synthetic-demo-$name.xml \
    --dataset $dataset --database corporate_intelligence_e2e --mode ALL_RECORDS \
    --started-by "demo"
done
```

Each import prints a summary. The June files report `COMPLETE_FILE_SCAN_WITH_QUARANTINED_RECORDS`
and the October files `COMPLETE_PUBLISHED_DATASET`.

**3. Explore the relationships.** Launch the dashboard. It opens on **Investigate** with nothing
selected, under a banner marking the demonstration database. Search for *Synthetic Harbour
Holdings plc*.

- *Network:* Harbour and its two consolidating parents. The quarantined LEI does not
  appear anywhere.
- *Evidence:* GLEIF's validation classification (`ENTITY_SUPPLIED_ONLY`). That is GLEIF's own
  label, not a CRIE corroboration finding.
- *Research leads* tab: **Selected network** lists both relationships. Choose *Synthetic Harbour
  Holdings plc → Synthetic Parent Group AG*. Once opened, an investigation is listed first as
  *Case #N*. The page shows:
  - the date the data is valid as of (the file's ContentDate);
  - which import batch created the row;
  - in **GLEIF source history**, both observations. The accounting periods are reported as
    "The published records differ (not a verified corporate change)", the ultimate parent as
    reported in one file and absent from the other, and the two legal names side by side.

**4. Open the investigation.** Under the network, choose the relationship and click **Investigate
this relationship**; My Investigations opens on it. Once ownership is installed, opening one
needs a signed-in or local user. Add a note, tick the acknowledgement that the row comes from an import batch whose
wider history has not been reviewed, and click **Save investigation**. Saving without the
acknowledgement is refused. The case then opens, with its sections in a row: Case and notes,
Relationship and GLEIF history, Sources and assessments, Claims, identities and corrections, and
Reports.

**5. Register three sources** under **Sources and assessments**, in **Register a new source**:
1. *Synthetic Harbour Holdings plc annual report 2024*;
2. *Synthetic Markets Regulator filing 2025*;
3. *Synthetic Newswire article on the 2025 filing*.

Any `https://` URL works, for example `https://example.invalid/...`. Then open **Claims,
identities and corrections** and use **Record a claim from a registered source** three times:

| Source | Entity | Metric | Year | Period end | Value | Location |
|---|---|---|---|---|---|---|
| Annual report 2024 | Synthetic Harbour Holdings plc | `parent_interest_pct` | 2024 | 2024-12-31 | 06 | Page 12 |
| Regulator filing 2025 | Synthetic Harbour Holdings plc | `parent_interest_pct` | 2025 | 2025-12-31 | 60 | Section 4 |
| Newswire article | Synthetic Harbour Holdings plc | `parent_interest_pct` | 2025 | 2025-12-31 | 60 | Paragraph 2 |

The "06" is a deliberate transcription error. Alternatively,
`scripts/demo_synthetic_case.py claims --case-id <N>` records the same three claims. The 2025
agreement shows as `INDEPENDENCE_UNVERIFIED`: two sources agree, but nothing establishes that they
are independent.

**6. Review, correct and assess:**

- **Review a source.** Select the regulator filing, set it to `SUPPORTS`, and give your reasoning.
  Without reasoning, the review is refused. **Assessment history** now lists the earlier
  `NOT_REVIEWED` entry and the current one.
- **Correct the claim.** In **Claim corrections**, open *Record a correction or restoration* and
  correct the 2024 claim from 06 to 60, citing the
  annual report, and confirm the acknowledgement. The original claim stays recorded as superseded.
- **Record source independence.** In **Source relationships and independence**, record that the
  newswire article is a `REPUBLICATION` of the regulator filing (basis `DOCUMENTED`). The 2025
  agreement becomes `CONSISTENT_RELATED_SOURCES`, which is still not corroboration.
- **Record the identity.** In **Claim subjects and legal-entity identity**, record "Synthetic
  Harbour Holdings plc" as `AMBIGUOUS` between two candidate LEIs. Then record it as `MAPPED`,
  superseding the first entry. A confirmed mapping requires stated limitations.

**7. Generate and verify a report** (snapshot version 4). Write it outside `reports/runs/`, which is
kept for real report runs:

```zsh
MYSQL_DATABASE=corporate_intelligence_e2e .venv/bin/python -m src.generate_versioned_report \
  --case-id <N> --previous-year 2024 --current-year 2025 --snapshot-version 4 \
  --output-root /tmp/crie-demo/runs
.venv/bin/python -c "from src.generate_versioned_report import verify_run; \
  print(verify_run('/tmp/crie-demo/runs/case_<N>/<run id>'))"     # [] means intact
```

What to expect in the generated `report.md`:
- The 2024 and 2025 values compare as `UNCHANGED`, because the corrected value is used.
- No claim is counted as corroborated.
- Section I lists the identity mappings and the global GLEIF context, kept apart from the case's
  evidence. Every GLEIF history reads `INSUFFICIENT_EVIDENCE` for any corporate change.

**8. Optional: register the run.** Register it in the end-to-end database with
`scripts/demo_synthetic_case.py register --case-id <N> --run-id <run id> --runs-root
/tmp/crie-demo/runs`. The dashboard's **Report History** panel reads run files from
`CRIE_RUNS_ROOT` (default `reports/runs/`): start the dashboard with
`CRIE_RUNS_ROOT=/tmp/crie-demo/runs` to verify and read this run there, or use `verify_run`
(step 7).

**9. Clean up.** This deletes every demonstration row and nothing else:

```zsh
.venv/bin/python scripts/demo_synthetic_case.py cleanup
```

Then restore `MYSQL_DATABASE` in `.env`.

## 4. Architecture

CRIE is a Python application with three layers:
- **`src/`** — the domain logic and command-line tools;
- **`app/`** — a Streamlit interface;
- **MySQL** — the system of record.

The database enforces the audit rules itself, so they don't depend on application code:
- append-only tables, whose triggers reject UPDATE and DELETE;
- supersession checks;
- foreign keys.

**GLEIF ingestion** (`src/gleif_xml.py`, `src/gleif_rr.py`, `src/gleif_lei2.py`,
`src/gleif_import.py`, `src/gleif_observations.py`):
- A hardened streaming XML parser reads each file twice: one pass to hash and count it, one to
  import it.
- Each record is stored under a canonical SHA-256. Imports run in restartable, leased batches
  with recorded coverage.
- Invalid identifiers go to quarantine.
- This importer writes only to the test and end-to-end databases.

**History** (`src/relationship_history.py`, `src/relationship_context.py`):
- Compares published observations field by field.
- Only completed import batches count.
- The corporate-change determination is always `INSUFFICIENT_EVIDENCE`.

**Evidence** (`src/source_assessments.py`, `src/claim_corrections.py`, `src/claim_subjects.py`,
`src/claim_reconciliation.py`, `src/comparison_rules.py`):
- source independence;
- claim lineage;
- identity mappings;
- reconciliation of agreeing and conflicting claims.

**Reports** (`src/evidence_snapshot.py`, `src/report_renderer.py`, `src/generate_versioned_report.py`,
`src/report_registry.py`, `src/report_verification.py`):
- A report is rendered only from a snapshot read in one consistent read-only transaction, and
  serialized as canonical JSON.
- Snapshot versions 1 to 4 each project exactly onto earlier versions. The default is version 3,
  and version 4 is used only when requested.
- Each run directory holds `report.md` and `manifest.json` (the snapshot, the hashes and the
  generator's Git commit), and is never overwritten.
- Registered runs are recorded in append-only registry tables.
- Run directories are retained outside Git (`reports/runs/` is ignored); hosting keeps them on a
  persistent volume (`docs/PUBLIC_DEMO.md`).

**Interface** (`app/`):
- `dashboard.py` is the entry point. It loads the data once and defines the four pages;
  `navigation.py` holds the state shared between pages.
- `investigations.py` lists the user's cases by ID and loads each case's own anchor row. Its
  sections reuse one module each: case management, GLEIF history (`review_queue.py`,
  `gleif_history_ui.py`), source registry, disclosures with identity and corrections, and report
  history.
- `access.py` decides who may see and change an investigation. `publishing.py` creates report
  versions and publications. `published.py` is the Published Investigations page.

## 5. Evidence limitations

What CRIE records is only as good as the sources and the investigator's judgement. In particular:

- **Consolidation is not ownership.** A GLEIF accounting-consolidation relationship means that the
  entity is reported as consolidated in the parent's financial statements. It is not evidence of
  direct ownership or managerial control. The network is a sample, not a complete ownership tree.
- **GLEIF data is largely self-reported.** GLEIF's validation classifications (for example
  `ENTITY_SUPPLIED_ONLY`, `FULLY_CORROBORATED`) are GLEIF's labels. CRIE never treats them, or any
  GLEIF record, as independent corroboration of a claim. Global GLEIF context in a report is kept
  separate from the case's evidence.
- **Dates and absences are not events.** A file's ContentDate is the time its data is valid as of,
  not the date of any corporate change. When published records differ, CRIE does not conclude
  that, or when, anything changed. A relationship missing from a later file is described by what
  that absence can mean, never as evidence that it ended. Quarantine makes absence undetermined.
- **Corroboration needs recorded independence.** Agreement counts as corroboration only between
  sources whose independent origin the investigator has recorded (`CONSISTENT_SOURCES`). Repeated
  claims, related or republished sources, and sources of unknown independence never corroborate.
  CRIE does not determine independence itself.
- **Nothing is identified automatically.** Claim subjects are mapped to legal entities only by the
  investigator. A matching GLEIF name is a hint, not proof of identity, and a mapping says nothing
  about ownership or control.
- **Assessments are manual.** The analyst case assessment is entered by hand and is not computed
  from the evidence. Unlike source reviews, its earlier values are not yet kept as history.
- **Report checks have limits:**
  - **INTACT** means only that the stored files match their hashes, not that the disclosures are
    correct.
  - **STALE** means only that the evidence has changed since the report.
  - **Reproducibility** can be checked only for runs generated from a clean, committed working
    tree. Otherwise it reports `CODE_VERSION_UNAVAILABLE`.

Current operational limitations:
- Claims are entered one at a time in the dashboard. The bulk importer
  `src/import_disclosure_claims.py` is fixed to case 1 and a local claims file,
  `data/derived/disclosure_facts.csv`, which is private research and not distributed.
- The observation-history GLEIF importer runs only against the test and end-to-end databases.
- Report files are read from `reports/runs/`, or from `CRIE_RUNS_ROOT` when set, and live on that
  disk. Hosting without persistent storage keeps only the files shipped with the application.
- The analyst case assessment keeps no history of earlier values.

## 6. Tests

```zsh
.venv/bin/python -m pytest -q                              # unit and interface tests
RUN_CRI_DB_TEST=1 .venv/bin/python -m pytest -q -rs         # including MySQL integration tests
```

The MySQL tests write only to `corporate_intelligence_test` and `corporate_intelligence_e2e`, check
the database identity first, and remove the synthetic rows they create. One test, the trigger-order
check, needs the `TRIGGER` privilege; without it, that test is skipped.

## 7. Copyright, data and dependencies

**Application code.** Copyright (c) 2026 Ayna Musayeva. All rights reserved. The code is published
for reading and evaluation. No open-source licence is granted, so copying, modifying or
redistributing it needs the author's permission.

**GLEIF data.** CRIE ingests Legal Entity Identifier (LEI-CDF) and relationship (RR-CDF) files
published by the Global Legal Entity Identifier Foundation (GLEIF). GLEIF makes LEI data available
under the CC0 1.0 public-domain dedication; see the [LEI Data Terms of
Use](https://www.gleif.org/en/meta/lei-data-terms-of-use). CRIE is not affiliated with or endorsed
by GLEIF, and the GLEIF name is GLEIF's trademark. No GLEIF bulk files are included; download them
from GLEIF. The GLEIF-format files under `tests/fixtures/` and those written by
`scripts/demo_synthetic_case.py` are synthetic.

**No other datasets are included.** The application ingests GLEIF data only. No corporate-registry,
sanctions, offshore-leaks or securities-filing datasets are part of this repository or its
workflows. Such datasets carry their own licences, some non-commercial or share-alike, and
publicly accessible records are not automatically free to redistribute.

**Private research is not included.** Investigation records, case claims, reports and databases
from the author's own research are not part of this repository. The public demonstration uses
synthetic companies, documents and GLEIF files only.

**Dependencies.** The packages in `requirements.txt` keep their own licences. Most are permissive
(Apache-2.0, BSD, MIT, PSF). `mysql-connector-python` is GPLv2 with Oracle's FOSS licence exception.
The repository lists these packages but does not redistribute them.
