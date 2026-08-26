.PHONY: blank-preflight blank-up blank-down blank-build blank-image-audit blank-typecheck blank-e2e blank-check

BLANK_ENV_FILE ?= .env.blank
BLANK_CHECK_ENV_FILE ?= .env.blank.example
BLANK_COMPOSE = docker compose --env-file $(BLANK_ENV_FILE) --file docker-compose.yml
BLANK_PROJECT_ARGS ?=
BLANK_CHECK_PROJECT ?=
BLANK_CHECK_FRONTEND_PORT ?=
BLANK_CHECK_BACKEND_PORT ?=

blank-preflight:
	@$(BLANK_COMPOSE) $(BLANK_PROJECT_ARGS) config --format json | python3 -c 'import json, sys; value = json.load(sys.stdin)["services"]["blank-backend"]["environment"].get("BLANK_POSTGRES_PASSWORD", ""); valid = len(value) >= 16 and not any(marker in value.lower() for marker in ("replace-with-", "change-before-deploy", "changeme")); sys.exit(0 if valid else "BLANK_POSTGRES_PASSWORD must be at least 16 characters and must not use a public example")'

blank-up: blank-preflight
	$(BLANK_COMPOSE) up -d --build --wait --wait-timeout 180
	$(BLANK_COMPOSE) ps

blank-down:
	$(BLANK_COMPOSE) down

blank-build:
	$(BLANK_COMPOSE) build blank-backend blank-frontend

blank-image-audit:
	@set -eu; \
		audit_compose='$(BLANK_COMPOSE) $(BLANK_PROJECT_ARGS)'; \
		backend_image="$$($$audit_compose images -q blank-backend)"; \
		frontend_image="$$($$audit_compose images -q blank-frontend)"; \
		test -n "$$backend_image"; \
		test -n "$$frontend_image"; \
		test "$$(docker image inspect --format '{{.Config.User}}' "$$backend_image")" = "blank"; \
		test "$$(docker image inspect --format '{{.Config.User}}' "$$frontend_image")" = "10001:10001"; \
		test "$$(docker image inspect --format '{{json .Config.Healthcheck.Test}}' "$$frontend_image")" != "null"; \
		docker run --rm --entrypoint sh "$$backend_image" -ec 'test ! -e /app/app; test ! -e /app/alembic; test ! -e /app/platform_tests; test ! -e /app/vendor; ! python -m pip show pytest >/dev/null 2>&1'; \
		docker run --rm --entrypoint sh "$$frontend_image" -ec 'test ! -e /app/src; test ! -e /app/tests; test ! -e /app/.next; test ! -e /app/server.js; test ! -e /app/apps/blank/tests; test ! -e /app/node_modules/.bin/playwright; test ! -e /app/node_modules/.bin/tsc; test ! -e /app/node_modules/.bin/eslint'

blank-typecheck:
	pnpm --dir frontend blank:typecheck

blank-e2e:
	pnpm --dir frontend blank:e2e

blank-check:
	@set -eu; \
		check_project='$(BLANK_CHECK_PROJECT)'; \
		if [ -z "$$check_project" ]; then check_project="easyframe-check-$$$$"; fi; \
		frontend_port='$(BLANK_CHECK_FRONTEND_PORT)'; \
		backend_port='$(BLANK_CHECK_BACKEND_PORT)'; \
		if [ -z "$$frontend_port" ] || [ -z "$$backend_port" ]; then \
			set -- $$(python3 -c 'import socket; sockets = [socket.socket() for _ in range(2)]; [item.bind(("127.0.0.1", 0)) for item in sockets]; print(*(item.getsockname()[1] for item in sockets))'); \
			if [ -z "$$frontend_port" ]; then frontend_port="$$1"; fi; \
			if [ -z "$$backend_port" ]; then backend_port="$$2"; fi; \
		fi; \
		export BLANK_FRONTEND_PORT="$$frontend_port"; \
		export BLANK_BACKEND_PORT="$$backend_port"; \
		export BLANK_WEBAUTHN_RP_ID="127.0.0.1"; \
		export BLANK_WEBAUTHN_ORIGINS="http://127.0.0.1:$$frontend_port"; \
		export BLANK_RUNTIME_ENV="test"; \
		export BLANK_LOCAL_AUTH_MODE="development"; \
		set -- $$(python3 -c 'import base64, secrets; print(secrets.token_hex(24), secrets.token_hex(32), secrets.token_hex(24), base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'); \
		export BLANK_POSTGRES_PASSWORD="$${BLANK_POSTGRES_PASSWORD:-check-$$1}"; \
		export BLANK_JWT_SECRET="$${BLANK_JWT_SECRET:-$$2}"; \
		export BLANK_ADMIN_PASSWORD="$${BLANK_ADMIN_PASSWORD:-check-$$3}"; \
		export BLANK_INTEGRATION_ENVELOPE_KEY="$${BLANK_INTEGRATION_ENVELOPE_KEY:-$$4}"; \
		check_compose='docker compose --env-file $(BLANK_CHECK_ENV_FILE) --file docker-compose.yml --project-name '"$$check_project"; \
		cleanup() { $$check_compose down --volumes --remove-orphans --rmi local; }; \
		trap cleanup EXIT INT TERM; \
		cleanup; \
		$$check_compose config --format json | python3 -c 'import json, sys; value = json.load(sys.stdin)["services"]["blank-backend"]["environment"].get("BLANK_POSTGRES_PASSWORD", ""); valid = len(value) >= 16 and not any(marker in value.lower() for marker in ("replace-with-", "change-before-deploy", "changeme")); sys.exit(0 if valid else "BLANK_POSTGRES_PASSWORD must be at least 16 characters and must not use a public example")'; \
		$$check_compose up -d --build --wait --wait-timeout 180 blank-postgres blank-backend blank-frontend; \
		$(MAKE) blank-image-audit BLANK_ENV_FILE="$(BLANK_CHECK_ENV_FILE)" BLANK_PROJECT_ARGS="--project-name $$check_project"; \
		$$check_compose exec -T blank-backend alembic -c blank_app/alembic.ini current --check-heads; \
		$$check_compose run --rm --build blank-backend-tests; \
		curl --fail --silent --show-error "http://127.0.0.1:$$backend_port/health"; \
		$$check_compose exec -T blank-backend python -c 'import json, os, urllib.request; payload = json.dumps({"username": os.environ["BLANK_ADMIN_USERNAME"], "password": os.environ["BLANK_ADMIN_PASSWORD"]}).encode(); login = urllib.request.Request("http://127.0.0.1:8000/api/v1/auth/login", data=payload, headers={"Content-Type": "application/json"}); token = json.load(urllib.request.urlopen(login))["accessToken"]; me = urllib.request.Request("http://127.0.0.1:8000/api/v1/auth/me", headers={"Authorization": "Bearer " + token}); assert json.load(urllib.request.urlopen(me))["name"] == os.environ["BLANK_ADMIN_USERNAME"]'; \
		curl --fail --silent --show-error "http://127.0.0.1:$$frontend_port/zh-CN/login" >/dev/null; \
		$(MAKE) blank-typecheck; \
		BLANK_E2E_FRONTEND_URL="http://127.0.0.1:$$frontend_port" $(MAKE) blank-e2e
