"""共享 EasyAuth permission snapshot HTTP client。"""

from __future__ import annotations

from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from enterprise_platform.authz.core import EasyAuthPermissionSnapshot
from enterprise_platform.safe_http import UnsafeOutboundUrlError, guarded_request


class EasyAuthClientError(RuntimeError):
    pass


class EasyAuthForbiddenError(EasyAuthClientError):
    pass


class EasyAuthPermissionClient:
    def __init__(
        self,
        *,
        base_url: str,
        app_key: str,
        auth_mode: Literal["static_app_token", "oauth2_client_credentials"] | str,
        credential: str,
        timeout: float = 5,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.app_key = app_key
        self.auth_mode = auth_mode
        self.credential = credential
        self.timeout = timeout
        self._test_client = (
            httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)
            if transport is not None
            else None
        )

    def close(self) -> None:
        if self._test_client is not None:
            self._test_client.close()

    def __enter__(self) -> EasyAuthPermissionClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def fetch_permission_snapshot(self, user_id: str) -> EasyAuthPermissionSnapshot:
        if not self.base_url or not self.app_key or not self.credential:
            raise EasyAuthClientError("EasyAuth is not configured")
        if self.auth_mode != "static_app_token":
            raise EasyAuthClientError("EasyAuth OAuth token provider is not configured")
        try:
            path = f"/api/v1/apps/{quote(self.app_key, safe='')}/users/{quote(user_id, safe='')}/permissions"
            if self._test_client is not None:
                response = self._test_client.get(path, headers={"Authorization": f"Bearer {self.credential}"})
            else:
                response = guarded_request(
                    "GET",
                    f"{self.base_url}{path}",
                    headers={"Authorization": f"Bearer {self.credential}"},
                    timeout=self.timeout,
                )
        except (httpx.RequestError, UnsafeOutboundUrlError) as exc:
            raise EasyAuthClientError("EasyAuth permission query failed") from exc
        if response.status_code == 401:
            raise EasyAuthClientError("EasyAuth app credential is unauthorized")
        if response.status_code == 403:
            raise EasyAuthForbiddenError("EasyAuth app credential is forbidden")
        if response.status_code >= 500:
            raise EasyAuthClientError("EasyAuth upstream service failed")
        if response.status_code >= 400:
            raise EasyAuthClientError(f"EasyAuth permission query returned HTTP {response.status_code}")
        try:
            snapshot = EasyAuthPermissionSnapshot.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise EasyAuthClientError("EasyAuth permission response is malformed") from exc
        if snapshot.app_key != self.app_key or snapshot.user_id != user_id:
            raise EasyAuthForbiddenError("EasyAuth response identity mismatch")
        return snapshot
