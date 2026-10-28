#!/bin/sh
# Railway start command: Streamlit on the port Railway assigns, on all interfaces.
# Local development is unchanged: .venv/bin/python -m streamlit run app/dashboard.py
set -eu
exec python -m streamlit run app/dashboard.py \
    --server.address 0.0.0.0 \
    --server.port "${PORT:-8501}" \
    --server.headless true \
    --browser.gatherUsageStats false
