"""把权限 manifest 推送到 EasyAuth,使内置「超级管理员」组覆盖该应用的全部权限。

宿主无关,不导入数据库模型。HTTP 走 ``EasyAuthHttp``(与 NotifyClient 相同)。
``store.load_state`` / ``save_state`` 只发生在请求前后,调用方不要在 HTTP 期间握着事务。

``ManifestSyncTarget(base_url, app_key, token, public_base_url=None)``
    EasyAuth 目的地与静态 token。``public_base_url`` 仅在 https 时放进请求体的 ``base_url``。

``ManifestSyncStore``
    ``load_state() -> dict | None``、``save_state(state: dict) -> None``。
    状态整份覆盖。键:``content_hash``、``schema_version``、``status``(``ok`` / ``failed``)、
    ``base_url``、``app_key``(EasyAuth 目的地,不是应用对外地址)、成功时的
    ``template_version`` 与 ``catalog_version``、``synced_at``(UTC ISO-8601)。

``manifest_content_hash(manifest) -> str``
    排除顶层 ``schema_version`` 后的 sha256。规范 JSON:``sort_keys``、``ensure_ascii=False``、
    分隔符 ``(",", ":")``。

``sync_manifest(*, target, manifest, store, force=False, transport=None, timeout=5.0)``
    POST ``{base_url}/api/v1/apps/{app_key}/manifest-sync``,Bearer token。
    无记录时用 manifest 自带 ``schema_version``(非法则 1);哈希未变时沿用已存版本;
    哈希变化时推 ``max(本地 schema_version, 已存 schema_version + 1)``。
    未 ``force`` 且 status 为 ``ok`` 且哈希与目的地都未变,则不发 HTTP。
    401/403/429/5xx 与网络错误不写状态。请求带了 ``base_url`` 且收到 422 时,去掉该字段再试一次。
    其余 4xx 把该哈希与目的地记为 ``failed``;之后的成功会覆盖。
    409 的 ``error.message`` 含「已导入版本 (N)」(括号前有空格,JSON 解码后才能匹配)。
    只再试一次,版本取 ``max(本次 + 1, 已存 + 1, N + 1)``。

``ManifestSyncScheduler(run, *, thread_name="easyauth-manifest-sync")``
    ``schedule(force=False)`` 起守护线程。同一实例单飞;进行中的调用把 ``force`` 按或并入一次补跑。
    ``schedule`` 与 ``run`` 的异常都被吞掉。测试运行时跳不跳由宿主决定。

接入::

    class Store:
        def load_state(self) -> dict | None:
            return read_json_setting("easyauth_manifest_sync")  # 短事务

        def save_state(self, state: dict) -> None:
            write_json_setting("easyauth_manifest_sync", state)  # 短事务

    def push(force: bool = False) -> None:
        target = ManifestSyncTarget(base_url, app_key, token, public_base_url)
        sync_manifest(target=target, manifest=build_manifest(), store=Store(), force=force)

    scheduler = ManifestSyncScheduler(push)

    def on_startup() -> None:
        if os.getenv("APP_ENV") != "test":
            scheduler.schedule()
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx

from enterprise_platform.easyauth.credentials import (
    STATIC_APP_TOKEN,
    EasyAuthCredential,
    EasyAuthCredentialError,
    EasyAuthHttp,
    EasyAuthProtocolError,
    EasyAuthTransportError,
    error_code_of,
    response_object,
)
from enterprise_platform.safe_http import UnsafeOutboundUrlError

logger = logging.getLogger(__name__)

_MANIFEST_PATH = "/manifest-sync"
_HASH = "content_hash"
_VERSION = "schema_version"
_STATUS = "status"
_BASE_URL = "base_url"
_APP_KEY = "app_key"
_TEMPLATE = "template_version"
_CATALOG = "catalog_version"
_SYNCED_AT = "synced_at"
_OK = "ok"
_FAILED = "failed"
_BODY_MANIFEST = "manifest"
# EasyAuth 报文是「已导入版本 (N)」(括号前有空格);兼容无空格。必须先 json() 解码。
_DOWNSTREAM_VERSION_RE = re.compile(r"已导入版本\s*\(\s*(\d+)\s*\)")
_RETRYABLE_STATUS = frozenset({401, 403, 429})
_Kind = Literal["ok", "failed", "retryable"]


@dataclass(frozen=True, slots=True)
class ManifestSyncTarget:
    """EasyAuth 目的地。``token`` 不进入 repr。"""

    base_url: str
    app_key: str
    token: str = field(repr=False)
    public_base_url: str | None = None


class ManifestSyncStore(Protocol):
    """宿主持久化同步状态。每次读写自行开关短事务。"""

    def load_state(self) -> dict[str, Any] | None: ...

    def save_state(self, state: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class ManifestSyncResult:
    """``skipped`` 表示未发 HTTP;``pushed`` 表示 EasyAuth 接受了本次 POST。"""

    skipped: bool
    pushed: bool
    content_hash: str | None = None
    schema_version: int | None = None
    status: str | None = None
    already_up_to_date: bool | None = None
    template_version: int | None = None
    catalog_version: int | None = None


@dataclass(frozen=True, slots=True)
class _Prepared:
    target: ManifestSyncTarget
    manifest: dict[str, Any]
    content_hash: str
    schema_version: int
    stored_schema_version: int | None
    base_url: str
    app_key: str


@dataclass(frozen=True, slots=True)
class _Attempt:
    kind: _Kind
    schema_version: int
    already_up_to_date: bool | None = None
    template_version: int | None = None
    catalog_version: int | None = None


def manifest_content_hash(manifest: Mapping[str, Any]) -> str:
    """排除 ``schema_version`` 的规范 JSON sha256,版本变化不改变哈希。"""

    content = {key: value for key, value in manifest.items() if key != _VERSION}
    canonical = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sync_manifest(
    *,
    target: ManifestSyncTarget,
    manifest: Mapping[str, Any],
    store: ManifestSyncStore,
    force: bool = False,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 5.0,
) -> ManifestSyncResult:
    """按内容哈希推送 manifest。可重试失败不写状态;不向调用方抛 HTTP 错误。"""

    state = _recorded(store.load_state())
    digest = manifest_content_hash(manifest)
    if _should_skip(state, digest, target, force=force):
        logger.debug("EasyAuth manifest 未变化,跳过推送")
        return _skipped(digest, state)
    prepared = _prepare(target, manifest, state, digest)
    attempt = _push(prepared, transport=transport, timeout=timeout)
    if attempt.kind != "retryable":
        store.save_state(_state_payload(prepared, attempt))
        _log_ok(attempt)
    return _to_result(prepared, attempt)


class ManifestSyncScheduler:
    """进程内单飞的守护线程。进行中再次 ``schedule`` 只记一次补跑,``force`` 按或合并。"""

    def __init__(self, run: Callable[[bool], object], *, thread_name: str = "easyauth-manifest-sync") -> None:
        self._run = run
        self._thread_name = thread_name
        self._guard = threading.Lock()
        self._running = False
        self._pending = False
        self._pending_force = False

    def schedule(self, force: bool = False) -> None:
        """启动或并入补跑。永不向调用方抛错。"""

        try:
            self._start(force)
        except Exception:
            logger.warning("EasyAuth manifest 同步调度失败", exc_info=True)

    def _start(self, force: bool) -> None:
        with self._guard:
            if self._running:
                self._pending = True
                self._pending_force = self._pending_force or force
                return
            self._running = True
        self._spawn(force)

    def _spawn(self, force: bool) -> None:
        try:
            threading.Thread(target=self._worker, args=(force,), name=self._thread_name, daemon=True).start()
        except Exception:
            self._mark_stopped()
            raise

    def _worker(self, force: bool) -> None:
        current = force
        try:
            while True:
                self._invoke(current)
                pending, current = self._claim_pending()
                if not pending:
                    return
        except Exception:
            logger.warning("EasyAuth manifest 同步线程失败", exc_info=True)
            self._mark_stopped()

    def _invoke(self, force: bool) -> None:
        try:
            self._run(force)
        except Exception:
            logger.warning("EasyAuth manifest 同步失败", exc_info=True)

    def _claim_pending(self) -> tuple[bool, bool]:
        with self._guard:
            if not self._pending:
                self._running = False
                return False, False
            force = self._pending_force
            self._pending = False
            self._pending_force = False
            return True, force

    def _mark_stopped(self) -> None:
        with self._guard:
            self._running = False


def _recorded(state: object) -> dict[str, Any] | None:
    if not isinstance(state, dict) or not state:
        return None
    return dict(state)


def _should_skip(state: dict[str, Any] | None, digest: str, target: ManifestSyncTarget, *, force: bool) -> bool:
    if force or state is None or state.get(_STATUS) != _OK:
        return False
    if state.get(_HASH) != digest:
        return False
    return _same_destination(state, target)


def _same_destination(state: dict[str, Any], target: ManifestSyncTarget) -> bool:
    stored_base = str(state.get(_BASE_URL) or "").strip().rstrip("/")
    stored_app = str(state.get(_APP_KEY) or "").strip()
    return stored_base == _destination_base(target) and stored_app == _destination_app(target)


def _prepare(
    target: ManifestSyncTarget,
    manifest: Mapping[str, Any],
    state: dict[str, Any] | None,
    digest: str,
) -> _Prepared:
    version = _next_schema_version(manifest, state, digest)
    return _Prepared(
        target=target,
        manifest=_with_version(manifest, version),
        content_hash=digest,
        schema_version=version,
        stored_schema_version=_stored_version(state),
        base_url=_destination_base(target),
        app_key=_destination_app(target),
    )


def _next_schema_version(manifest: Mapping[str, Any], state: dict[str, Any] | None, digest: str) -> int:
    """哈希未变沿用已存版本;变了则 max(本地版本, 已存版本 + 1)。无记录时用本地版本。"""

    local = _schema_version_floor(manifest.get(_VERSION))
    stored = _stored_version(state)
    if stored is not None and state is not None and state.get(_HASH) == digest:
        return stored
    if stored is None:
        return local
    return max(local, stored + 1)


def _schema_version_floor(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 1
    return value


def _stored_version(state: dict[str, Any] | None) -> int | None:
    if state is None:
        return None
    return _positive_int(state.get(_VERSION))


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _with_version(manifest: Mapping[str, Any], version: int) -> dict[str, Any]:
    copied = dict(manifest)
    copied[_VERSION] = version
    return copied


def _destination_base(target: ManifestSyncTarget) -> str:
    return target.base_url.strip().rstrip("/")


def _destination_app(target: ManifestSyncTarget) -> str:
    return target.app_key.strip()


def _push(
    prepared: _Prepared,
    *,
    transport: httpx.BaseTransport | None,
    timeout: float,
) -> _Attempt:
    try:
        http = EasyAuthHttp(_credential(prepared.target), timeout=timeout, transport=transport)
        response, version = _exchange(http, prepared)
    except (EasyAuthTransportError, EasyAuthCredentialError, UnsafeOutboundUrlError) as exc:
        logger.warning("EasyAuth manifest 推送失败: %s", exc)
        return _Attempt("retryable", prepared.schema_version)
    return _interpret(response, version)


def _credential(target: ManifestSyncTarget) -> EasyAuthCredential:
    return EasyAuthCredential(
        base_url=_destination_base(target),
        app_key=_destination_app(target),
        auth_mode=STATIC_APP_TOKEN,
        credential=target.token,
    )


def _exchange(http: EasyAuthHttp, prepared: _Prepared) -> tuple[httpx.Response, int]:
    response = _send(http, prepared.manifest, prepared.target.public_base_url)
    if response.status_code != 409:
        return response, prepared.schema_version
    healed = _higher_version(
        attempted=prepared.schema_version,
        stored=prepared.stored_schema_version,
        downstream=_downstream_version(response),
    )
    logger.warning("EasyAuth manifest 版本冲突,改用 schema_version=%s 重试一次", healed)
    retried = _send(http, _with_version(prepared.manifest, healed), prepared.target.public_base_url)
    return retried, healed


def _higher_version(*, attempted: int, stored: int | None, downstream: int | None) -> int:
    version = attempted + 1
    if stored is not None:
        version = max(version, stored + 1)
    if downstream is not None:
        version = max(version, downstream + 1)
    return version


def _send(http: EasyAuthHttp, manifest: dict[str, Any], public_base_url: str | None) -> httpx.Response:
    body = _request_body(manifest, public_base_url)
    response = http.request("POST", _MANIFEST_PATH, json=body)
    if response.status_code == 422 and _BASE_URL in body:
        logger.warning("EasyAuth manifest 拒绝 base_url,改为不带地址重试")
        return http.request("POST", _MANIFEST_PATH, json={_BODY_MANIFEST: manifest})
    return response


def _request_body(manifest: dict[str, Any], public_base_url: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {_BODY_MANIFEST: manifest}
    public = _https_base_url(public_base_url)
    if public:
        body[_BASE_URL] = public
    return body


def _https_base_url(raw: str | None) -> str:
    text = (raw or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return ""
    return text


def _downstream_version(response: httpx.Response) -> int | None:
    parsed = _parse_imported_version(_error_message(response))
    if parsed is not None:
        return parsed
    return _parse_imported_version(response.text)


def _error_message(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    message = error.get("message")
    return message if isinstance(message, str) else None


def _parse_imported_version(detail: str | None) -> int | None:
    if not detail:
        return None
    match = _DOWNSTREAM_VERSION_RE.search(detail)
    if match is None:
        return None
    return int(match.group(1))


def _interpret(response: httpx.Response, version: int) -> _Attempt:
    status = response.status_code
    if status < 400:
        return _attempt_from_success(response, version)
    logger.warning("EasyAuth manifest 推送被拒绝: HTTP %s %s", status, error_code_of(response))
    if status in _RETRYABLE_STATUS or status >= 500:
        return _Attempt("retryable", version)
    return _Attempt("failed", version)


def _attempt_from_success(response: httpx.Response, version: int) -> _Attempt:
    try:
        payload = response_object(response)
    except EasyAuthProtocolError as exc:
        logger.warning("EasyAuth manifest 响应无效: %s", exc)
        return _Attempt("retryable", version)
    outcome = _outcome(payload)
    if outcome is None:
        logger.warning("EasyAuth manifest 响应字段无效")
        return _Attempt("retryable", version)
    already, template_version, catalog_version = outcome
    return _Attempt("ok", version, already, template_version, catalog_version)


def _outcome(payload: Mapping[str, Any]) -> tuple[bool, int, int] | None:
    already = payload.get("already_up_to_date")
    template_version = _as_int(payload.get(_TEMPLATE))
    catalog_version = _as_int(payload.get(_CATALOG))
    if not isinstance(already, bool) or template_version is None or catalog_version is None:
        return None
    return already, template_version, catalog_version


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _state_payload(prepared: _Prepared, attempt: _Attempt) -> dict[str, Any]:
    payload: dict[str, Any] = {
        _HASH: prepared.content_hash,
        _VERSION: attempt.schema_version,
        _STATUS: _OK if attempt.kind == "ok" else _FAILED,
        _BASE_URL: prepared.base_url,
        _APP_KEY: prepared.app_key,
        _SYNCED_AT: datetime.now(UTC).isoformat(),
    }
    if attempt.kind != "ok":
        return payload
    payload[_TEMPLATE] = attempt.template_version
    payload[_CATALOG] = attempt.catalog_version
    return payload


def _to_result(prepared: _Prepared, attempt: _Attempt) -> ManifestSyncResult:
    status = None
    if attempt.kind == "ok":
        status = _OK
    elif attempt.kind == "failed":
        status = _FAILED
    return ManifestSyncResult(
        skipped=False,
        pushed=attempt.kind == "ok",
        content_hash=prepared.content_hash,
        schema_version=attempt.schema_version,
        status=status,
        already_up_to_date=attempt.already_up_to_date,
        template_version=attempt.template_version,
        catalog_version=attempt.catalog_version,
    )


def _skipped(digest: str, state: dict[str, Any] | None) -> ManifestSyncResult:
    return ManifestSyncResult(skipped=True, pushed=False, content_hash=digest, schema_version=_stored_version(state))


def _log_ok(attempt: _Attempt) -> None:
    if attempt.kind != "ok":
        return
    logger.info(
        "EasyAuth manifest 已推送 schema_version=%s already_up_to_date=%s template_version=%s catalog_version=%s",
        attempt.schema_version,
        attempt.already_up_to_date,
        attempt.template_version,
        attempt.catalog_version,
    )


__all__ = [
    "ManifestSyncResult",
    "ManifestSyncScheduler",
    "ManifestSyncStore",
    "ManifestSyncTarget",
    "manifest_content_hash",
    "sync_manifest",
]
