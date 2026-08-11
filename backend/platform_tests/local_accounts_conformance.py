"""local-accounts v2 宿主接线一致性检查。

本模块是下游宿主的可导入测试合同，不是 pytest 测试模块；函数名刻意使用
``check_*``，宿主应在自己的 ``test_*.py`` 中构造 ``ConformanceHost`` 并逐项调用。
本模块不得依赖或导入 ``blank_app``。

宿主适配器必须满足以下约束（可直接实现 Protocol，也可用结构类型）：

* ``app`` 必须是已经挂好生产路由、中间件和异常处理器的 FastAPI 实例；套件不接受
  只挂内核 router 的测试替身。
* ``api_prefix`` 是宿主 API 前缀，默认语义为 ``/api/v1``；``auth_me_path``、
  ``login_path`` 均为相对该前缀的路径。没有可测密码登录路由时，``login_path``
  必须为 ``None``，仅跳过登录资格检查，不跳过管理 API 与 ``/auth/me`` 检查。
* ``admin_username`` / ``admin_password`` 必须对应测试环境 bootstrap 管理员配置；
  ``ensure_admin`` 必须把该本地账号恢复为 active、非过期、无需改密的可用测试
  操作员，并返回其稳定 id。套件不会读取具体宿主环境变量。
* ``create_raw_account`` 必须绕过管理 API 直接提交一行并返回 ``RawAccount``；
  ``grants`` 按 LocalGrant 对象（``code`` + ``scope``）原样落库，``external=True``
  必须创建无本地密码、非本地管理员的 SSO 行；``expires_at`` 必须保留时区。
* ``issue_session`` 必须走宿主真实 session 签发实现并返回裸 token；该 token 经 ``app``
  的真实认证中间件解析，禁止在测试中 override 当前用户依赖。
* ``set_account_expiry`` 必须直接提交 expiry 变更，供“既有会话立即失效”检查使用。
* ``only_usable_admin`` 是可恢复的上下文管理器；进入后指定 id 是唯一 active、未过期
  的本地管理员，退出时必须恢复其它管理员，避免污染后续用例。
* ``audit_rows`` 必须查询已经提交的宿主审计表，并按写入顺序返回匹配 target id 的
  ``AuditRecord``；变更响应返回后立刻可见审计行，证明业务写与审计写同事务提交。
* ``add_catalog_permission`` / ``remove_catalog_permission`` 必须直接提交、删除 active
  权限目录行；套件用唯一测试 code 验证标准/高风险权限与 v2
  ``supportedScopes`` / ``grantableScopes`` 投影，而不是假设宿主目录内容或表名。
* ``remove_raw_account`` 必须幂等删除指定测试账号及其宿主依赖数据和相关测试审计；
  账号不存在时也必须成功。``seed_account_dependents`` 必须为指定本地账号各创建一行应随账号删除的宿主依赖数据；
  ``count_account_dependents`` 返回相同的非空计数键集合，供套件证明删除级联接线。
* ``local_auth_mode`` 是可恢复的上下文管理器，必须让真实登录与 session 资格判定读取
  指定模式；仅当 ``login_path`` 非空时调用。五态名称不同的宿主通过 ``login_modes``
  映射 enabled、break-glass、disabled 三种合同语义。
* ``extract_login_token(response_json)`` 与 ``extract_me(response_json)`` 是可选响应提取
  hook；未实现时分别按平铺 ``accessToken`` 与平铺 ``/auth/me`` 对象解释。宿主可用它们
  适配既有 ``data.accessToken`` / ``data`` envelope，不得为跑套件改写生产 API。

所有方法面向隔离的测试数据库。套件会创建、修改和删除账号；用户名自带随机后缀，
但宿主仍须负责测试事务/数据库清理。HTTP 状态与 JSON 字段断言是 v2 对外合同，适配器
不得在返回前改写响应。
"""

from __future__ import annotations

import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Mapping, Protocol, Sequence

from fastapi import FastAPI
from fastapi.testclient import TestClient


LocalGrant = Mapping[str, str]
STANDARD_GRANT: dict[str, str] = {"code": "accounts.local.view", "scope": "ALL"}
MANAGE_GRANT: dict[str, str] = {"code": "accounts.local.manage", "scope": "ALL"}


@dataclass(frozen=True)
class RawAccount:
    id: str
    username: str
    password: str | None


@dataclass(frozen=True)
class AuditRecord:
    action: str
    before_data: Mapping[str, object] | None
    after_data: Mapping[str, object] | None


@dataclass(frozen=True)
class LoginModes:
    enabled: str = "enabled"
    break_glass: str = "break_glass"
    disabled: str = "disabled"


class ConformanceHost(Protocol):
    app: FastAPI
    api_prefix: str
    auth_me_path: str
    login_path: str | None
    login_modes: LoginModes
    admin_username: str
    admin_password: str

    def ensure_admin(self) -> RawAccount: ...

    def create_raw_account(
        self,
        *,
        username: str,
        password: str | None,
        is_admin: bool = False,
        grants: Sequence[LocalGrant] = (),
        expires_at: datetime | None = None,
        must_change_password: bool = False,
        external: bool = False,
    ) -> RawAccount: ...

    def issue_session(self, account_id: str) -> str: ...

    def set_account_expiry(self, account_id: str, expires_at: datetime | None) -> None: ...

    def remove_raw_account(self, account_id: str) -> None: ...

    def only_usable_admin(self, account_id: str) -> AbstractContextManager[None]: ...

    def audit_rows(self, account_id: str) -> Sequence[AuditRecord]: ...

    def add_catalog_permission(
        self,
        *,
        code: str,
        supported_scopes: Sequence[str],
        risk_level: str = "standard",
    ) -> None: ...

    def remove_catalog_permission(self, code: str) -> None: ...

    def seed_account_dependents(self, account_id: str) -> None: ...

    def count_account_dependents(self, account_id: str) -> Mapping[str, int]: ...

    def local_auth_mode(self, mode: str) -> AbstractContextManager[None]: ...


HOST_CONTRACT_MEMBERS = (
    "app",
    "api_prefix",
    "auth_me_path",
    "login_path",
    "login_modes",
    "admin_username",
    "admin_password",
    "ensure_admin",
    "create_raw_account",
    "issue_session",
    "set_account_expiry",
    "remove_raw_account",
    "only_usable_admin",
    "audit_rows",
    "add_catalog_permission",
    "remove_catalog_permission",
    "seed_account_dependents",
    "count_account_dependents",
    "local_auth_mode",
)
_HOST_METHODS = frozenset(HOST_CONTRACT_MEMBERS[7:])


def check_host_contract_member(host: ConformanceHost, member: str) -> None:
    """给宿主测试提供逐成员、易定位失败的 adapter 形状检查。"""

    assert member in HOST_CONTRACT_MEMBERS, f"未知 ConformanceHost 成员: {member}"
    value = getattr(host, member, None)
    assert value is not None or member == "login_path", f"ConformanceHost 缺少 {member}"
    if member in _HOST_METHODS:
        assert callable(value), f"ConformanceHost.{member} 必须可调用"
    elif member == "app":
        assert isinstance(value, FastAPI), "ConformanceHost.app 必须是 FastAPI 实例"
    elif member in {"api_prefix", "auth_me_path", "admin_username", "admin_password"}:
        assert isinstance(value, str) and value, f"ConformanceHost.{member} 必须是非空字符串"
    elif member == "login_path":
        assert value is None or isinstance(value, str), "ConformanceHost.login_path 必须是字符串或 None"
    elif member == "login_modes":
        assert isinstance(value, LoginModes), "ConformanceHost.login_modes 必须是 LoginModes"


def _suffix() -> str:
    return uuid.uuid4().hex[:10]


def _url(host: ConformanceHost, path: str) -> str:
    return f"{host.api_prefix.rstrip('/')}/{path.lstrip('/')}"


def _headers(host: ConformanceHost, account_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {host.issue_session(account_id)}"}


def _assert_status(response, expected: int) -> None:
    assert response.status_code == expected, response.text


def _remove_created_accounts(host: ConformanceHost, account_ids: Sequence[str]) -> None:
    """Best-effort every tracked account, while still surfacing teardown failures."""

    first_error: BaseException | None = None
    for account_id in reversed(account_ids):
        try:
            host.remove_raw_account(account_id)
        except BaseException as exc:  # pragma: no cover - exercised by downstream host failures
            first_error = first_error or exc
    if first_error is not None:
        raise first_error


def _remove_created_catalog_permissions(host: ConformanceHost, codes: Sequence[str]) -> None:
    first_error: BaseException | None = None
    for code in reversed(codes):
        try:
            host.remove_catalog_permission(code)
        except BaseException as exc:  # pragma: no cover - exercised by downstream host failures
            first_error = first_error or exc
    if first_error is not None:
        raise first_error


def _default_extract_login_token(response_json: Mapping[str, object]) -> str:
    token = response_json.get("accessToken")
    assert isinstance(token, str) and token, "登录响应缺少非空 accessToken"
    return token


def _default_extract_me(response_json: Mapping[str, object]) -> Mapping[str, object]:
    return response_json


def _extract_login_token(host: ConformanceHost, response_json: Mapping[str, object]) -> str:
    extractor = getattr(host, "extract_login_token", _default_extract_login_token)
    token = extractor(response_json)
    assert isinstance(token, str) and token, "extract_login_token 必须返回非空字符串"
    return token


def _extract_me(host: ConformanceHost, response_json: Mapping[str, object]) -> Mapping[str, object]:
    extractor = getattr(host, "extract_me", _default_extract_me)
    me = extractor(response_json)
    assert isinstance(me, Mapping), "extract_me 必须返回对象"
    return me


def check_router_mount_and_permission_gates(host: ConformanceHost) -> None:
    """证明 router 已挂载，view/manage 与强制改密门禁来自宿主真实认证链。"""

    suffix = _suffix()
    collection = _url(host, "/local-accounts")
    payload = {
        "username": f"conformance-created-{suffix}",
        "password": "Conformance-created-password-42!",
    }
    created_account_ids: list[str] = []

    with TestClient(host.app) as client:
        try:
            no_grants = host.create_raw_account(
                username=f"conformance-none-{suffix}", password="Conformance-none-password-42!"
            )
            created_account_ids.append(no_grants.id)
            viewer = host.create_raw_account(
                username=f"conformance-view-{suffix}",
                password="Conformance-view-password-42!",
                grants=[STANDARD_GRANT],
            )
            created_account_ids.append(viewer.id)
            manager = host.create_raw_account(
                username=f"conformance-manager-{suffix}",
                password="Conformance-manager-password-42!",
                grants=[STANDARD_GRANT, MANAGE_GRANT],
            )
            created_account_ids.append(manager.id)
            must_change = host.create_raw_account(
                username=f"conformance-change-{suffix}",
                password="Conformance-change-password-42!",
                grants=[STANDARD_GRANT, MANAGE_GRANT],
                must_change_password=True,
            )
            created_account_ids.append(must_change.id)
            target = host.create_raw_account(
                username=f"conformance-gate-target-{suffix}", password="Conformance-target-password-42!"
            )
            created_account_ids.append(target.id)
            matrix = (
                ("GET", collection, None, True),
                ("POST", collection, payload, False),
                ("GET", f"{collection}/{target.id}", None, True),
                ("PATCH", f"{collection}/{target.id}", {"email": "gate@example.com"}, False),
                ("DELETE", f"{collection}/{target.id}", None, False),
                (
                    "POST",
                    f"{collection}/{target.id}/password",
                    {"password": "Conformance-reset-password-43!", "mustChangePassword": False},
                    False,
                ),
                (
                    "PUT",
                    f"{collection}/{target.id}/permissions",
                    {"permissions": [STANDARD_GRANT], "expectedVersion": 0},
                    False,
                ),
                ("DELETE", f"{collection}/{target.id}/totp", None, False),
                ("GET", f"{collection}/permission-catalog", None, True),
            )

            assert client.get(collection).status_code in {401, 403}
            no_grants_headers = _headers(host, no_grants.id)
            viewer_headers = _headers(host, viewer.id)
            for method, path, body, view_allowed in matrix:
                kwargs = {"json": body} if body is not None else {}
                _assert_status(client.request(method, path, headers=no_grants_headers, **kwargs), 403)
                _assert_status(
                    client.request(method, path, headers=viewer_headers, **kwargs),
                    200 if view_allowed else 403,
                )
            _assert_status(client.post(collection, headers=_headers(host, viewer.id), json=payload), 403)
            created_response = client.post(collection, headers=_headers(host, manager.id), json=payload)
            _assert_status(created_response, 201)
            created_account_ids.append(created_response.json()["id"])
            blocked = client.get(collection, headers=_headers(host, must_change.id))
            _assert_status(blocked, 403)
            assert blocked.json()["detail"]["code"] == "PASSWORD_CHANGE_REQUIRED"
        finally:
            _remove_created_accounts(host, created_account_ids)


def check_crud_local_grants_cas_and_audit(host: ConformanceHost) -> None:
    """证明 CRUD、会话撤销、级联、LocalGrant CAS 与精确脱敏审计序列。"""

    admin = host.ensure_admin()
    headers = _headers(host, admin.id)
    suffix = _suffix()
    username = f"conformance-crud-{suffix}"
    collection = _url(host, "/local-accounts")
    standard_code = f"conformance.standard.{suffix}"
    high_code = f"conformance.high.{suffix}"
    grants = [
        {"code": standard_code, "scope": "ALL"},
        {"code": high_code, "scope": "ALL"},
    ]
    original_password = "Conformance-crud-password-42!"
    updated_password = "Conformance-crud-password-43!"
    created_account_ids: list[str] = []
    created_catalog_codes: list[str] = []

    with TestClient(host.app) as client:
        try:
            host.add_catalog_permission(code=standard_code, supported_scopes=["ALL"], risk_level="standard")
            created_catalog_codes.append(standard_code)
            host.add_catalog_permission(code=high_code, supported_scopes=["ALL"], risk_level="high")
            created_catalog_codes.append(high_code)
            created_response = client.post(
                collection,
                headers=headers,
                json={
                    "username": username,
                    "email": f"{username}@example.com",
                    "password": original_password,
                    "mustChangePassword": False,
                    "permissions": grants,
                },
            )
            _assert_status(created_response, 201)
            created = created_response.json()
            account_id = created["id"]
            created_account_ids.append(account_id)
            assert created["permissions"] == grants
            assert all(set(grant) == {"code", "scope"} for grant in created["permissions"])

            duplicate = client.post(
                collection,
                headers=headers,
                json={"username": username, "password": original_password},
            )
            _assert_status(duplicate, 409)

            listing = client.get(f"{collection}?search={username}", headers=headers)
            _assert_status(listing, 200)
            assert listing.json()["meta"]["total"] == 1
            detail_path = f"{collection}/{account_id}"
            detail = client.get(detail_path, headers=headers)
            _assert_status(detail, 200)
            assert detail.json()["permissions"] == grants

            updated = client.patch(
                detail_path,
                headers=headers,
                json={"email": f"updated-{username}@example.com", "uiLocale": "en-US"},
            )
            _assert_status(updated, 200)
            assert updated.json()["uiLocale"] == "en-US"

            permission_update = client.put(
                f"{detail_path}/permissions",
                headers=headers,
                json={"permissions": [MANAGE_GRANT], "expectedVersion": created["localGrantsVersion"]},
            )
            _assert_status(permission_update, 200)
            assert permission_update.json()["permissions"] == [MANAGE_GRANT]

            stale = client.put(
                f"{detail_path}/permissions",
                headers=headers,
                json={"permissions": [], "expectedVersion": created["localGrantsVersion"]},
            )
            _assert_status(stale, 409)

            password_session = host.issue_session(account_id)
            reset = client.post(
                f"{detail_path}/password",
                headers=headers,
                json={"password": updated_password, "mustChangePassword": False},
            )
            _assert_status(reset, 200)
            _assert_status(
                client.get(
                    _url(host, host.auth_me_path),
                    headers={"Authorization": f"Bearer {password_session}"},
                ),
                401,
            )

            totp_session = host.issue_session(account_id)
            _assert_status(client.delete(f"{detail_path}/totp", headers=headers), 200)
            _assert_status(
                client.get(
                    _url(host, host.auth_me_path),
                    headers={"Authorization": f"Bearer {totp_session}"},
                ),
                401,
            )

            host.seed_account_dependents(account_id)
            before_counts = dict(host.count_account_dependents(account_id))
            assert before_counts and all(count > 0 for count in before_counts.values())

            deleted = client.delete(detail_path, headers=headers)
            _assert_status(deleted, 204)
            _assert_status(client.get(detail_path, headers=headers), 404)
            assert dict(host.count_account_dependents(account_id)) == {
                key: 0 for key in before_counts
            }

            audits = host.audit_rows(account_id)
            assert [row.action for row in audits] == [
                "accounts.local.create",
                "accounts.local.update",
                "accounts.local.permissions.set",
                "accounts.local.password.reset",
                "accounts.local.totp.disable",
                "accounts.local.delete",
            ]
            for row in audits:
                assert row.after_data is None or row.after_data.get("targetAccountId") == account_id
                audit_payload = f"{row.before_data!r}{row.after_data!r}".lower()
                assert original_password.lower() not in audit_payload
                assert updated_password.lower() not in audit_payload
        finally:
            try:
                _remove_created_accounts(host, created_account_ids)
            finally:
                _remove_created_catalog_permissions(host, created_catalog_codes)


def check_operator_target_guards(host: ConformanceHost) -> None:
    """抽查 delegated×target、isAdmin、高风险、自操作与最后管理员保护矩阵。"""

    suffix = _suffix()
    high_code = f"conformance.operator-high.{suffix}"
    collection = _url(host, "/local-accounts")
    created_account_ids: list[str] = []
    created_catalog_codes: list[str] = []

    with TestClient(host.app) as client:
        try:
            host.add_catalog_permission(code=high_code, supported_scopes=["ALL"], risk_level="high")
            created_catalog_codes.append(high_code)
            manager = host.create_raw_account(
                username=f"conformance-delegated-{suffix}",
                password="Conformance-delegated-password-42!",
                grants=[STANDARD_GRANT, MANAGE_GRANT],
            )
            created_account_ids.append(manager.id)
            normal = host.create_raw_account(
                username=f"conformance-target-{suffix}", password="Conformance-target-password-42!"
            )
            created_account_ids.append(normal.id)
            privileged = host.create_raw_account(
                username=f"conformance-high-{suffix}",
                password="Conformance-high-password-42!",
                grants=[{"code": high_code, "scope": "ALL"}],
            )
            created_account_ids.append(privileged.id)
            target_admin = host.create_raw_account(
                username=f"conformance-admin-{suffix}",
                password="Conformance-admin-password-42!",
                is_admin=True,
            )
            created_account_ids.append(target_admin.id)
            superadmin = host.create_raw_account(
                username=f"conformance-superadmin-{suffix}",
                password="Conformance-superadmin-password-42!",
                is_admin=True,
            )
            created_account_ids.append(superadmin.id)
            headers = _headers(host, manager.id)
            _assert_status(
                client.patch(
                    f"{collection}/{normal.id}", headers=headers, json={"email": "allowed@example.com"}
                ),
                200,
            )
            _assert_status(client.patch(f"{collection}/{normal.id}", headers=headers, json={"isAdmin": True}), 422)
            for target in (privileged, target_admin):
                _assert_status(
                    client.patch(
                        f"{collection}/{target.id}",
                        headers=headers,
                        json={"email": f"blocked-{target.id}@example.com"},
                    ),
                    403,
                )
            _assert_status(client.patch(f"{collection}/{manager.id}", headers=headers, json={"active": False}), 403)
            # A delegated actor is rejected before the invariant is evaluated. Use a
            # real local superadmin and make it the sole usable admin, then demote a
            # second admin: a broken usable_local_admin_count (for example, always 0)
            # now fails this request instead of being masked by the delegated guard.
            with host.only_usable_admin(superadmin.id):
                _assert_status(
                    client.patch(
                        f"{collection}/{target_admin.id}",
                        headers=_headers(host, superadmin.id),
                        json={"isAdmin": False},
                    ),
                    200,
                )
        finally:
            try:
                _remove_created_accounts(host, created_account_ids)
            finally:
                _remove_created_catalog_permissions(host, created_catalog_codes)


def check_expiry_catalog_and_sso_visibility(host: ConformanceHost) -> None:
    """证明 expiry 时区合同、expired 摘要、目录 v2 投影与 SSO 行隐藏。"""

    admin = host.ensure_admin()
    headers = _headers(host, admin.id)
    collection = _url(host, "/local-accounts")
    suffix = _suffix()
    managed_code = f"conformance.managed-only.{suffix}"
    created_account_ids: list[str] = []
    created_catalog_codes: list[str] = []

    with TestClient(host.app) as client:
        try:
            expired = host.create_raw_account(
                username=f"conformance-expired-{suffix}",
                password="Conformance-expired-password-42!",
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
            )
            created_account_ids.append(expired.id)
            external = host.create_raw_account(
                username=f"conformance-sso-{suffix}", password=None, external=True
            )
            created_account_ids.append(external.id)
            # 宿主 lifespan 可能会同步/重建目录，测试行必须在真实启动流程完成后写入。
            host.add_catalog_permission(code=managed_code, supported_scopes=["MANAGED_USERS"])
            created_catalog_codes.append(managed_code)
            past = client.post(
                collection,
                headers=headers,
                json={
                    "username": f"conformance-past-{suffix}",
                    "password": "Conformance-past-password-42!",
                    "expiresAt": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                },
            )
            _assert_status(past, 422)
            naive = client.post(
                collection,
                headers=headers,
                json={
                    "username": f"conformance-naive-{suffix}",
                    "password": "Conformance-naive-password-42!",
                    "expiresAt": "2030-01-01T00:00:00",
                },
            )
            _assert_status(naive, 422)
            _assert_status(
                client.patch(
                    f"{collection}/{expired.id}",
                    headers=headers,
                    json={"expiresAt": "2030-01-01T00:00:00"},
                ),
                422,
            )
            summary = client.get(f"{collection}/{expired.id}", headers=headers)
            _assert_status(summary, 200)
            assert summary.json()["expired"] is True
            external_matrix = (
                ("GET", f"{collection}/{external.id}", None),
                ("PATCH", f"{collection}/{external.id}", {"active": False}),
                ("DELETE", f"{collection}/{external.id}", None),
                (
                    "POST",
                    f"{collection}/{external.id}/password",
                    {"password": "Conformance-external-password-42!"},
                ),
                (
                    "PUT",
                    f"{collection}/{external.id}/permissions",
                    {"permissions": [STANDARD_GRANT], "expectedVersion": 0},
                ),
                ("DELETE", f"{collection}/{external.id}/totp", None),
            )
            for method, path, body in external_matrix:
                kwargs = {"json": body} if body is not None else {}
                _assert_status(client.request(method, path, headers=headers, **kwargs), 404)

            catalog = client.get(f"{collection}/permission-catalog", headers=headers)
            _assert_status(catalog, 200)
            items = catalog.json()["data"]
            required = {
                "code",
                "nameZh",
                "nameEn",
                "groupKey",
                "riskLevel",
                "supportedScopes",
                "grantableScopes",
            }
            assert items and all(set(item) == required for item in items)
            assert all(item["nameZh"] and item["nameEn"] and item["groupKey"] for item in items)
            managed = next(item for item in items if item["code"] == managed_code)
            assert managed["supportedScopes"] == ["MANAGED_USERS"]
            assert managed["grantableScopes"] == []
            local_view = next(item for item in items if item["code"] == "accounts.local.view")
            local_manage = next(item for item in items if item["code"] == "accounts.local.manage")
            assert local_view["groupKey"] == "accounts"
            assert local_view["riskLevel"] == "standard"
            assert local_view["supportedScopes"] == ["ALL"]
            assert local_view["grantableScopes"] == ["ALL"]
            assert local_manage["groupKey"] == "accounts"
            assert local_manage["riskLevel"] == "high"
            assert local_manage["supportedScopes"] == ["ALL"]
            assert local_manage["grantableScopes"] == ["ALL"]
        finally:
            try:
                _remove_created_accounts(host, created_account_ids)
            finally:
                _remove_created_catalog_permissions(host, created_catalog_codes)


def check_auth_me_identity(host: ConformanceHost) -> None:
    """证明宿主 /auth/me 暴露稳定 accountId 与规范本地超管谓词。"""

    admin = host.ensure_admin()
    with TestClient(host.app) as client:
        response = client.get(_url(host, host.auth_me_path), headers=_headers(host, admin.id))
    _assert_status(response, 200)
    me = _extract_me(host, response.json())
    assert me["accountId"] == admin.id
    assert me["isLocalSuperadmin"] is True


def check_live_login_eligibility(host: ConformanceHost) -> None:
    """若宿主提供真实密码登录路由，证明 enabled/expiry/break-glass/disabled 资格。"""

    if host.login_path is None:
        return
    suffix = _suffix()
    password = "Conformance-login-password-42!"
    login_url = _url(host, host.login_path)
    me_url = _url(host, host.auth_me_path)
    created_account_ids: list[str] = []

    def login(client: TestClient, account: RawAccount):
        return client.post(login_url, json={"username": account.username, "password": account.password})

    with TestClient(host.app) as client:
        try:
            normal = host.create_raw_account(
                username=f"conformance-login-{suffix}", password=password
            )
            created_account_ids.append(normal.id)
            admin = host.create_raw_account(
                username=f"conformance-login-admin-{suffix}", password=password, is_admin=True
            )
            created_account_ids.append(admin.id)
            with host.local_auth_mode(host.login_modes.enabled):
                accepted = login(client, normal)
                _assert_status(accepted, 200)
                old_headers = {"Authorization": f"Bearer {_extract_login_token(host, accepted.json())}"}
                host.set_account_expiry(normal.id, datetime.now(UTC) - timedelta(seconds=1))
                _assert_status(login(client, normal), 401)
                _assert_status(client.get(me_url, headers=old_headers), 401)
                host.set_account_expiry(normal.id, None)

            with host.local_auth_mode(host.login_modes.enabled):
                normal_token = host.issue_session(normal.id)
            with host.local_auth_mode(host.login_modes.break_glass):
                _assert_status(login(client, normal), 401)
                _assert_status(client.get(me_url, headers={"Authorization": f"Bearer {normal_token}"}), 401)
                _assert_status(login(client, admin), 200)

            with host.local_auth_mode(host.login_modes.disabled):
                _assert_status(login(client, admin), 401)
        finally:
            _remove_created_accounts(host, created_account_ids)


CONFORMANCE_CHECKS = (
    check_router_mount_and_permission_gates,
    check_crud_local_grants_cas_and_audit,
    check_operator_target_guards,
    check_expiry_catalog_and_sso_visibility,
    check_auth_me_identity,
    check_live_login_eligibility,
)
