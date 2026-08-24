"""本地登录流程与失败额度管理。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypeVar

from fastapi import HTTPException, Request

from enterprise_platform.auth import AuthError
from enterprise_platform.ports import AccountPort
from enterprise_platform.rate_limit import (
    account_failure_admission,
    clear_login_failures,
    clear_second_factor_failures,
    record_login_failure,
    record_second_factor_failure,
    second_factor_failure_admission,
)
from enterprise_platform.schemas import (
    LoginRequest,
    LoginResponse,
    PasskeyLoginBeginRequest,
    PasskeyLoginBeginResponse,
    PasskeyLoginCompleteRequest,
)

_CredentialResult = TypeVar("_CredentialResult")
_LoginEvent = Callable[[str, str, str], None]
_SecondFactorAdmission = Callable[[str, str, Request], None]


def _facade():
    import importlib
    import sys

    return sys.modules.get("enterprise_platform.assembly") or importlib.import_module("enterprise_platform.assembly")


@dataclass(frozen=True, slots=True)
class LoginAdmissionGuard:
    """在同一账号条带锁内完成准入、凭据校验与失败额度维护。"""

    admit: Callable[[str, Request], None]

    def run(
        self,
        username: str,
        request: Request,
        credential_check: Callable[[], _CredentialResult],
        *,
        clear_on_success: bool,
    ) -> _CredentialResult:
        with account_failure_admission(username):
            self.admit(username, request)
            try:
                result = credential_check()
            except AuthError as exc:
                if _facade().is_credential_failure(exc):
                    record_login_failure(username)
                raise HTTPException(exc.status_code, exc.detail) from exc
            if clear_on_success:
                clear_login_failures(username)
        return result


@dataclass(slots=True)
class _SecondFactorState:
    used: bool = False
    failure_audited: bool = False


@contextmanager
def _second_factor_admission(
    account_id: str,
    *,
    username: str,
    request: Request,
    method: str,
    state: _SecondFactorState,
    admit: _SecondFactorAdmission,
    login_event: _LoginEvent,
) -> Iterator[None]:
    """保持二次验证准入、失败记账和成功清账的原子顺序。"""

    state.used = True
    with second_factor_failure_admission(account_id):
        admit(account_id, "login", request)
        try:
            yield
        except AuthError as exc:
            if exc.kind == "second_factor":
                record_second_factor_failure(account_id)
                login_event(username, "auth.login.second_factor_failure", method)
                state.failure_audited = True
            raise
        else:
            clear_second_factor_failures(account_id)


def _run_password_login(
    body: LoginRequest,
    request: Request,
    *,
    account: AccountPort,
    login_guard: LoginAdmissionGuard,
    admit_second_factor: _SecondFactorAdmission,
    login_event: _LoginEvent,
) -> LoginResponse:
    state = _SecondFactorState()

    def second_factor_admission(account_id: str):
        return _second_factor_admission(
            account_id,
            username=body.username,
            request=request,
            method="totp",
            state=state,
            admit=admit_second_factor,
            login_event=login_event,
        )

    def check() -> tuple[str, bool]:
        return _facade().authenticate_login(
            account,
            body.username,
            body.password,
            body.totp_code,
            before_second_factor=second_factor_admission,
        )

    try:
        token, must_change = login_guard.run(body.username, request, check, clear_on_success=True)
    except HTTPException as exc:
        if exc.status_code == 429:
            login_event(body.username, "auth.login.rate_limited", "totp" if state.used else "password")
        elif (
            not (isinstance(exc.detail, dict) and exc.detail.get("code") == "REQUIRE_SECOND_FACTOR") and not state.used
        ):
            login_event(body.username, "auth.login.failure", "password")
        raise
    login_event(body.username, "auth.login.success", "totp" if state.used else "password")
    return LoginResponse(access_token=token, must_change_password=must_change)


def _run_passkey_login_begin(
    body: PasskeyLoginBeginRequest,
    request: Request,
    *,
    account: AccountPort,
    login_guard: LoginAdmissionGuard,
    login_event: _LoginEvent,
) -> PasskeyLoginBeginResponse:
    def check():
        return _facade().begin_passkey_login(account, body.username, body.password)

    # begin 只是挑战下发,登录尚未完成:不消耗额度,也不提前 reset。
    try:
        _, challenge = login_guard.run(body.username, request, check, clear_on_success=False)
    except HTTPException as exc:
        login_event(
            body.username,
            "auth.login.rate_limited" if exc.status_code == 429 else "auth.login.failure",
            "password",
        )
        raise
    return PasskeyLoginBeginResponse(options=challenge.options, state_token=challenge.state_token)


def _run_passkey_login_complete(
    body: PasskeyLoginCompleteRequest,
    request: Request,
    *,
    account: AccountPort,
    login_guard: LoginAdmissionGuard,
    admit_second_factor: _SecondFactorAdmission,
    login_event: _LoginEvent,
) -> LoginResponse:
    state = _SecondFactorState()

    def passkey_admission(account_id: str):
        return _second_factor_admission(
            account_id,
            username=body.username,
            request=request,
            method="passkey",
            state=state,
            admit=admit_second_factor,
            login_event=login_event,
        )

    def check() -> tuple[str, bool]:
        return _facade().complete_passkey_login(
            account,
            body.username,
            body.password,
            body.state_token,
            body.credential,
            before_second_factor=passkey_admission,
        )

    try:
        token, must_change = login_guard.run(body.username, request, check, clear_on_success=True)
    except HTTPException as exc:
        if exc.status_code == 429:
            login_event(body.username, "auth.login.rate_limited", "passkey" if state.used else "password")
        elif not state.failure_audited:
            login_event(body.username, "auth.login.failure", "password")
        raise
    login_event(body.username, "auth.login.success", "passkey")
    return LoginResponse(access_token=token, must_change_password=must_change)
