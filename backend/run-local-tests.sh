#!/bin/sh
# Local pytest runner against the throwaway dev postgres (easyframe-dev-pg on 127.0.0.1:55433).
# Mirrors the env forced by `make blank-check`. Not for CI — blank-check remains the gate.
set -eu
cd "$(dirname "$0")"
# 库口令不写进仓库:给整条 BLANK_DATABASE_URL,或用 BLANK_DEV_PG_PASSWORD 指定本地 dev 库口令。
if [ -z "${BLANK_DATABASE_URL:-}" ]; then
	if [ -z "${BLANK_DEV_PG_PASSWORD:-}" ]; then
		echo "run-local-tests.sh: 需要 BLANK_DEV_PG_PASSWORD(本地 dev postgres 的口令),或直接提供 BLANK_DATABASE_URL。" >&2
		echo "起一次性 dev 库:" >&2
		echo "  docker run -d --name easyframe-dev-pg -p 127.0.0.1:55433:5432 \\" >&2
		echo '    -e POSTGRES_USER=enterprise -e POSTGRES_PASSWORD="$BLANK_DEV_PG_PASSWORD" \\' >&2
		echo "    -e POSTGRES_DB=enterprise_blank postgres:16" >&2
		exit 2
	fi
	BLANK_DATABASE_URL="postgresql+psycopg://enterprise:${BLANK_DEV_PG_PASSWORD}@127.0.0.1:55433/enterprise_blank"
fi
export BLANK_DATABASE_URL
export BLANK_RUNTIME_ENV="test"
export BLANK_LOCAL_AUTH_MODE="development"
export BLANK_ADMIN_USERNAME="admin"
# 其余口令每次运行现生成(不落仓库);需要复现固定值时由环境变量覆盖。
# 注意:这里不能用 `set --` 取值,脚本末尾要把 "$@" 原样透给 pytest。
export BLANK_JWT_SECRET="${BLANK_JWT_SECRET:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"
export BLANK_ADMIN_PASSWORD="${BLANK_ADMIN_PASSWORD:-check-$(python3 -c 'import secrets; print(secrets.token_hex(24))')}"
export BLANK_INTEGRATION_ENVELOPE_KEY="${BLANK_INTEGRATION_ENVELOPE_KEY:-$(python3 -c 'import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')}"
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
