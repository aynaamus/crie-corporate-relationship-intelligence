# Hosting CRIE on Railway: Deployment Guide

**Status: prepared for review. Nothing here has been deployed.** Each step that creates
accounts, databases or hosting needs the project owner's approval.

A hosted CRIE is the existing four-page application, running on Railway with a MySQL service in the
same Railway project, against a hosted copy of the synthetic demonstration database:
- **Without an account,** visitors can search companies, read the published example and verify its
  report. With one click they can start a **private sandbox** and use the whole workflow
  themselves: open investigations, register sources, record claims, assess, correct, map
  identities and create reports.
- **Sandbox work is private to whoever holds the sandbox's link,** and is never published.
- **Signed-in users,** if sign-in is configured, can also publish their investigations.
- **The synthetic example** is published under a system owner, so no visitor or user can change
  it.

## 1. What protects users' work

| Safeguard | How it works |
|---|---|
| Owner of each case | `investigation_cases.owner_subject`, set when a case is opened and never changeable (database trigger). A signed-in user's subject is `oidc:` plus a hash of the identity provider and account ID; no e-mail is stored. |
| Ownership checked on every write | Each write checks the case's stored owner on its own connection before writing (`app/access.py`). This covers case updates, sources, reviews, source metadata, independence assessments, identity mappings, claim corrections, report versions and publication. A user who is not the owner is refused even if the page were bypassed. |
| Private drafts | My Investigations lists only the user's own cases. Published Investigations shows only what was published: the chosen report, title and summary. A case's working records are never shown to others. |
| Publication checked in the database | `investigation_publications` is append-only. Its trigger refuses a publication unless the publisher owns the case and the report run belongs to it. A run is verified INTACT before it can be published. |
| No deletion | The hosted application's account has `SELECT, INSERT, UPDATE` only (`sql/demo/demo_accounts.sql`); users' work is never deleted. |
| Guest sandboxes | A guest is `guest:` plus a hash of a random token carried in the page's link; only the hash is stored. A sandbox holds at most five investigations, cannot publish, and is removed by `cleanup-guests` after the chosen age. |
| Demonstration database only | With `CRIE_HOSTED=1`, CRIE connects only to `corporate_intelligence_e2e`, requires the ownership schema and ignores the local single-user setting. |

## 2. What the deployment needs

| Item | Value |
|---|---|
| Entry point | `app/dashboard.py` |
| Start command | `sh deploy/start.sh` (set in `railway.json`). It runs Streamlit on `$PORT` and `0.0.0.0`, headless. |
| Health check | `/_stcore/health` |
| Python | 3.12 (`.python-version`); dependencies from `requirements.txt`; Railpack builder |
| MySQL | **8.4.13** (MySQL 8.4 LTS). The schema needs 8.0.18 or later: enforced `CHECK` constraints, `utf8mb4_0900` collations, `REGEXP_*` functions, ordered triggers, and `IDENTIFIED BY RANDOM PASSWORD` in the accounts script. CRIE was rehearsed on 8.4.13: schema, accounts, example build, two visitors and a restart. |
| Database | `corporate_intelligence_e2e`, created with `utf8mb4` and `utf8mb4_unicode_ci` |
| Report files | A Railway volume mounted at **`/data`**, with `CRIE_RUNS_ROOT=/data/runs`. Report generation, registration, integrity checks, report history and published reports all read and write there. |

Local development is unchanged: `.venv/bin/python -m streamlit run app/dashboard.py` with `.env`.

## 3. Railway project

Create one Railway project with two services.

**MySQL service.** Use the image `mysql:8.4.13`, with a volume at `/var/lib/mysql`. If you start
from Railway's MySQL template, check its image tag and set it to `mysql:8.4.13` before the service
first starts. A data directory created by a newer MySQL cannot be opened by an older one. Do not
set `MYSQL_DATABASE` on this service; the database is created in section 5 with its collation.

**Application service** (from the deployment repository, section 8):
- a volume mounted at `/data`;
- one replica, because a volume belongs to one instance;
- a public domain (Settings, then Networking), so visitors can reach it.

## 4. Application environment variables

Set these on the application service. Use reference variables, so the app reaches MySQL over
Railway's private network. Secret values go only into Railway's variables, never into the
repository.

| Variable | Value |
|---|---|
| `MYSQL_HOST` | `${{MySQL.RAILWAY_PRIVATE_DOMAIN}}` (the MySQL service's private name) |
| `MYSQL_PORT` | `3306` |
| `MYSQL_USER` | `crie_demo_app` |
| `MYSQL_PASSWORD` | the password printed for `crie_demo_app` in section 5 |
| `MYSQL_DATABASE` | `corporate_intelligence_e2e` |
| `CRIE_HOSTED` | `1` |
| `CRIE_RUNS_ROOT` | `/data/runs` |

`PORT` is set by Railway. `CRIE_SANDBOX` is on by default when hosted. Do not set
`CRIE_LOCAL_USER`; it is ignored when hosted anyway. In hosted mode CRIE ignores any `.env` file,
connects only to `corporate_intelligence_e2e`, and stops if the ownership schema is missing.

Sign-in for publishing users is optional and not configured by these steps. Visitors use
sandboxes, and the example is the published investigation.

## 5. Initialise the database (administrator, once)

Run these from a trusted machine with the `mysql` client and the MySQL service's root password.
Never put the password on the command line; `-p` asks for it.

**Access for initialisation only.** Installing the schema needs the `mysql` client, because the
scripts use `DELIMITER` blocks, and it needs this repository's SQL files. Neither Railway service has
both: the application container has no `mysql` client, and the MySQL container has no schema files.
So the current procedure cannot install the schema through `railway ssh`. Instead:
1. enable a TCP proxy on the MySQL service (Settings, then Networking) only for these steps;
2. use its host and port below;
3. remove the proxy as soon as step 3 is done.

Everything later (the example build and sandbox cleanup) runs inside the application service over
Railway's private network and needs no proxy.

1. Write the schema from the reviewed repository:

   ```zsh
   .venv/bin/python scripts/demo_synthetic_case.py schema demo_schema.sql
   ```

   It is the end-to-end scripts in order, ending with the ownership migration. The only change is
   the view's `DEFINER=root@localhost` clause, which is removed so the installing account owns the
   view.

2. Create the database, then install the schema:

   ```zsh
   mysql -h <proxy host> -P <proxy port> -u root -p \
     -e "CREATE DATABASE corporate_intelligence_e2e CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
   mysql -h <proxy host> -P <proxy port> -u root -p < demo_schema.sql
   ```

3. Create the two accounts. The output shows each generated password once; keep it somewhere
   private.

   ```zsh
   mysql -h <proxy host> -P <proxy port> -u root -p --table < sql/demo/demo_accounts.sql
   ```

   Enter `crie_demo_app`'s password into the application's `MYSQL_PASSWORD` variable. Keep
   `crie_demo_builder`'s password for step 6 and for sandbox cleanup.

Do not use `--force`. If a step fails, stop and inspect before running anything again. Then remove
the MySQL service's TCP proxy.

## 6. Build the published example (once)

The example's report files must be written to the application's volume, so the build runs inside
the application service. Deploy the application first, then open a shell in it with the Railway CLI
(`railway ssh`), and run:

```sh
read -rs MYSQL_PASSWORD && export MYSQL_PASSWORD     # paste the builder's password; it is not shown
MYSQL_USER=crie_demo_builder python scripts/demo_synthetic_case.py build --runs-root "$CRIE_RUNS_ROOT"
unset MYSQL_PASSWORD
```

`build` refuses to run if the example already exists. It records the README walkthrough with the
GLEIF importer and the same functions the dashboard uses:
- four synthetic GLEIF files;
- one investigation and three sources;
- three claims, one of them corrected;
- a review, an independence assessment and two identity mappings;
- the case's version 4 report, written under `/data/runs` and registered.

It then publishes the case, owned by `crie:demonstration`. In the hosted copy, the report records
its code version as not available, because the container holds no Git history. Integrity and
evidence currency are fully checked.

## 7. Removing old sandboxes

Sandboxes are removed by age with the builder account; the application's account cannot delete.
From the application shell, as in step 6:

```sh
read -rs MYSQL_PASSWORD && export MYSQL_PASSWORD
MYSQL_USER=crie_demo_builder python scripts/demo_synthetic_case.py cleanup-guests --older-than-hours 72
unset MYSQL_PASSWORD
```

Sandbox report files stay on the volume until removed by hand. They are small.

## 8. The deployment repository

Railway deploys from a Git repository, which should hold only the deployment files. This public
repository was produced that way and can be deployed directly. `deploy/package.zsh` copies a
checkout's files, tracked or new and not ignored, minus the paths in `deploy/exclude.txt`, into a
new directory:

```zsh
zsh deploy/package.zsh ../crie-deploy
```

**Left out of this repository (`deploy/exclude.txt`):**
- real SPP case data and historical report outputs (`data/`, `reports/`);
- GLEIF working samples;
- the production operations procedure;
- local backup tooling and its tests;
- the two tests that read the real case's data or private fixture;
- the local application account's grant script; a hosted installation does not use it;
- the legacy single-report generator, which reads the private case data;
- a browser download artifact.

Ignored files such as `.env` and `tests/fixtures/private/` are never copied.

**Kept:**
- `app/`, `src/`, `scripts/demo_synthetic_case.py` and `sql/`;
- `tests/` (the example build uses its synthetic GLEIF file writers);
- `README.md`, `docs/PUBLIC_DEMO.md`, `requirements.txt`, `.python-version`, `railway.json`,
  `deploy/` and `.streamlit/config.toml`.

The included `src/` modules and tests name the SPP/EPI companies and their public GLEIF LEIs where
the code and its examples require them. They hold no case data.

## 9. Checks after deploying

1. Open the public domain. The Investigate page shows **Try CRIE**.
2. On Published Investigations, open the example and choose **Verify this report**. It should read
   INTACT and CURRENT.
3. Start a sandbox, open an investigation, record a source and a claim, and create a report
   version.
4. Redeploy or restart the application service, then repeat step 2, and reopen the sandbox's link.
   Both reports should still verify, because they live on the volume.
