FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.blank.txt .
RUN pip install --no-cache-dir --requirement requirements.blank.txt \
    && python -m pip check

# Blank 生产镜像只包含共享企业平台内核、空白站宿主与其独立迁移。
COPY enterprise_platform/ enterprise_platform/
COPY blank_app/ blank_app/

ARG BLANK_RUNTIME_UID=10001
ARG BLANK_RUNTIME_GID=10001
RUN groupadd --gid "${BLANK_RUNTIME_GID}" blank \
    && useradd --uid "${BLANK_RUNTIME_UID}" --gid blank --home-dir /app --shell /usr/sbin/nologin blank \
    && chown -R blank:blank /app

EXPOSE 8000

USER blank

# Compose 之外直接运行镜像时也拒绝公开数据库密码；应用层继续验证 JWT/admin/principal。
CMD ["sh", "-ec", "password=${BLANK_POSTGRES_PASSWORD:-}; case \"$password\" in ''|*replace-with-*|*REPLACE-WITH-*|*change-before-deploy*|*CHANGE-BEFORE-DEPLOY*|*changeme*|*CHANGEME*) echo >&2 'BLANK_POSTGRES_PASSWORD must be at least 16 characters and must not use a public example'; exit 64;; esac; if [ \"${#password}\" -lt 16 ]; then echo >&2 'BLANK_POSTGRES_PASSWORD must be at least 16 characters and must not use a public example'; exit 64; fi; exec python -m blank_app.run"]

FROM runtime AS test

USER root

COPY requirements.blank-test.txt .
RUN pip install --no-cache-dir --requirement requirements.blank-test.txt \
    && python -m pip check
COPY platform_tests/ platform_tests/

USER blank

CMD ["python", "-m", "pytest", "-q", "platform_tests"]

# 默认 target 保持为不含测试代码和测试依赖的生产运行时。
FROM runtime AS production
