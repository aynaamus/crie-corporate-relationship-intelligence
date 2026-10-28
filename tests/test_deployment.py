
"""The Railway deployment files: start command, configuration and package contents."""

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_railway_starts_streamlit_on_the_assigned_port_and_all_interfaces():
    config = json.loads((ROOT / "railway.json").read_text())
    assert config["deploy"]["startCommand"] == "sh deploy/start.sh"
    assert config["deploy"]["healthcheckPath"] == "/_stcore/health"
    start = (ROOT / "deploy/start.sh").read_text()
    assert "streamlit run app/dashboard.py" in start
    assert '--server.port "${PORT:-8501}"' in start and "--server.address 0.0.0.0" in start
    assert (ROOT / ".python-version").read_text().strip() == "3.12"
    # Local configuration stays free of a fixed server address.
    assert "address" not in (ROOT / ".streamlit/config.toml").read_text()


def test_hosted_installations_ignore_a_dotenv_file():
    for name in ("app/dashboard.py", "app/case_management.py"):
        code = (ROOT / name).read_text()
        assert 'load_dotenv(ROOT / ".env", override=os.getenv("CRIE_HOSTED") != "1")' in code


@pytest.mark.skipif(not (ROOT / ".git").exists(),
                    reason="the package is assembled from a Git checkout of the development repository")
def test_package_leaves_out_case_data_and_keeps_what_the_build_needs(tmp_path):
    target = tmp_path / "package"
    subprocess.run(["zsh", str(ROOT / "deploy/package.zsh"), str(target)], check=True,
                   capture_output=True)
    files = {str(p.relative_to(target)) for p in target.rglob("*") if p.is_file()}

    for excluded in ("data/", "reports/", "docs/REGISTRY_ACTIVATION.md",
                     "scripts/backup_production_db.zsh", "tests/test_e2e_spp_workflow.py",
                     "tests/fixtures/private/", "companyfacts.zip-2.download/", ".env",
                     "sql/e2e/m23a_e2e_05_grant.sql"):
        assert not any(f == excluded or f.startswith(excluded) for f in files), excluded
    for needed in ("app/dashboard.py", "railway.json", "deploy/start.sh", ".python-version",
                   "requirements.txt", "scripts/demo_synthetic_case.py",
                   "tests/gleif_lei2_samples.py", "tests/gleif_rr_samples.py",
                   "tests/test_mysql_gleif_import.py", "sql/demo/demo_accounts.sql",
                   "sql/e2e/m24_e2e_investigation_ownership.sql"):
        assert needed in files, needed
    for name in files:
        text = (target / name).read_bytes()
        assert not re.search(rb"MYSQL_PASSWORD\s*=\s*['\"]?[A-Za-z0-9+@_/!$%^&*-]{6}", text), name
