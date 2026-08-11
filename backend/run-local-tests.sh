#!/bin/sh
# Local pytest runner against the throwaway dev postgres (easyframe-dev-pg on 127.0.0.1:55433).
# Mirrors the env forced by `make blank-check`. Not for CI — blank-check remains the gate.
set -eu
cd "$(dirname "$0")"
export BLANK_DATABASE_URL="postgresql+psycopg://enterprise:blank-check-database-password-2026@127.0.0.1:55433/enterprise_blank"
export BLANK_RUNTIME_ENV="test"
export BLANK_LOCAL_AUTH_MODE="development"
export BLANK_JWT_SECRET="blank-check-jwt-secret-0123456789abcdef"
export BLANK_ADMIN_USERNAME="admin"
export BLANK_ADMIN_PASSWORD="blank-check-admin-password-2026"
export BLANK_INTEGRATION_ENVELOPE_KEY="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
export BLANK_WEBAUTHN_RP_ID="localhost"
export BLANK_WEBAUTHN_ORIGINS="http://localhost:3000"
# platform_tests 假定全新数据库(blank-check 每次起新库);本地库每次运行前重置 schema。
.venv/bin/python - <<'PY'
import os
from sqlalchemy import create_engine, text

engine = create_engine(os.environ["BLANK_DATABASE_URL"])
with engine.begin() as conn:
    conn.execute(text("DROP SCHEMA public CASCADE"))
    conn.execute(text("CREATE SCHEMA public"))
PY
.venv/bin/python -m alembic -c blank_app/alembic.ini upgrade head
exec .venv/bin/python -m pytest -q platform_tests "$@"
