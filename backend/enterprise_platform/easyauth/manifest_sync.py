"""把权限 manifest 推送到 EasyAuth,使内置「超级管理员」组覆盖该应用的全部权限。

宿主无关,不导入数据库模型。HTTP 走 ``EasyAuthHttp``。状态读写只发生在请求前后。

``ManifestSyncTarget(base_url, app_key, token, public_base_url=None)``
    EasyAuth 目的地与静态 token。``public_base_url`` 仅在 https 时进入请求体 ``base_url``。

``ManifestSyncStore``
    ``load_state() -> dict | None``、``save_state(state) -> None``,整份覆盖。
    成功时写入 ``content_hash``、``schema_version``、``status=ok``、``base_url``、``app_key``、
    ``template_version``、``catalog_version``、``synced_at``。``schema_version`` /
    ``template_version`` 只来自成功响应;失败不覆盖上一份成功行。同一内容与目的地的
    422 另记 ``http_status=422`` 与 ``rejected_hash``,未 ``force`` 时不再 POST。

``manifest_content_hash``
    排除顶层 ``schema_version`` 的 sha256。规范 JSON:``sort_keys``、``ensure_ascii=False``、``(",", ":")``。

``sync_manifest``
    POST ``{base_url}/api/v1/apps/{app_key}/manifest-sync``。无已接受版本时用 manifest 自带
    ``schema_version``(非法则 1)。已接受且哈希未变则沿用该版本;变了则 ``max(本地, 已存 + 1)``。
    409 先改发报文中的已导入版本 N;仅当这次仍是 409 才发 N+1。幂等命中则保存响应里的
    ``template_version``。每次调用最多去掉一次 ``base_url``、做一次版本探测。
    401/403/429/5xx 与网络错误不写状态;由 ``ManifestSyncScheduler`` 调用时按 ``Retry-After``
    (没有则 60 秒)最多延迟重试一次。

``ManifestSyncScheduler``
    ``schedule(force=False)`` 起守护线程,单飞,进行中的 ``force`` 按或并入一次补跑。
    启动失败或线程崩溃时若已有补跑则再拉起。异常不向调用方抛出。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
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
    parse_retry_after_seconds,
    response_object,
)
from enterprise_platform.easyauth.manifest_scheduler import _DEFER, ManifestSyncScheduler
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
_HTTP_STATUS = "http_status"
_REJECTED_HASH = "rejected_hash"
_REJECTED_BASE = "rejected_base_url"
_REJECTED_APP = "rejected_app_key"
_OK = "ok"
_FAILED = "failed"
_BODY_MANIFEST = "manifest"
_UNPROCESSABLE = 422
# EasyAuth 成功同步 10 次/60s;manifest 429 本身不带 Retry-After。
_RETRY_AFTER_FALLBACK_SECONDS = 60
# 报文是「已导入版本 (N)」(括号前有空格)。必须先 json() 解码。
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
    base_url: str
    app_key: str


@dataclass(frozen=True, slots=True)
class _Attempt:
    kind: _Kind
    schema_version: int
    already_up_to_date: bool | None = None
    template_version: int | None = None
    catalog_version: int | None = None
    http_status: int | None = None
    retry_after: int | None = None


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
    _commit(store, state, prepared, attempt)
    return _to_result(prepared, attempt)


def _recorded(state: object) -> dict[str, Any] | None:
    if not isinstance(state, dict) or not state:
        return None
    return dict(state)


def _should_skip(state: dict[str, Any] | None, digest: str, target: ManifestSyncTarget, *, force: bool) -> bool:
    if force or state is None:
        return False
    if _same_422(state, digest, target):
        return True
    if state.get(_STATUS) != _OK or state.get(_HASH) != digest:
        return False
    return _same_destination(state, target)


def _same_422(state: dict[str, Any], digest: str, target: ManifestSyncTarget) -> bool:
    if state.get(_HTTP_STATUS) != _UNPROCESSABLE:
        return False
    rejected = state.get(_REJECTED_HASH)
    if isinstance(rejected, str):
        return rejected == digest and _matches(state.get(_REJECTED_BASE), state.get(_REJECTED_APP), target)
    return state.get(_HASH) == digest and _same_destination(state, target)


def _same_destination(state: dict[str, Any], target: ManifestSyncTarget) -> bool:
    return _matches(state.get(_BASE_URL), state.get(_APP_KEY), target)


def _matches(base: object, app: object, target: ManifestSyncTarget) -> bool:
    stored_base = str(base or "").strip().rstrip("/")
    stored_app = str(app or "").strip()
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
        base_url=_destination_base(target),
        app_key=_destination_app(target),
    )


def _next_schema_version(manifest: Mapping[str, Any], state: dict[str, Any] | None, digest: str) -> int:
    """无已接受版本时用 manifest 自带版本;已接受且哈希未变则沿用,否则在其上递增。"""

    local = _schema_version_floor(manifest.get(_VERSION))
    accepted = _accepted_version(state)
    if accepted is None or state is None:
        return local
    if state.get(_HASH) == digest:
        return accepted
    return max(local, accepted + 1)


def _schema_version_floor(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 1
    return value


def _accepted_version(state: dict[str, Any] | None) -> int | None:
    if state is None or state.get(_STATUS) != _OK:
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
    except EasyAuthTransportError as exc:
        logger.warning("EasyAuth manifest 推送失败: %s", exc)
        return _Attempt("retryable", prepared.schema_version, retry_after=_RETRY_AFTER_FALLBACK_SECONDS)
    except (EasyAuthCredentialError, UnsafeOutboundUrlError) as exc:
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


class _Poster:
    """一次触发内最多去掉一次 ``base_url``。"""

    def __init__(self, http: EasyAuthHttp, public_base_url: str | None) -> None:
        self._http = http
        self._public = public_base_url
        self.stripped = False

    def send(self, manifest: dict[str, Any]) -> httpx.Response:
        body = _request_body(manifest, None if self.stripped else self._public)
        response = self._http.request("POST", _MANIFEST_PATH, json=body)
        if response.status_code != _UNPROCESSABLE or self.stripped or _BASE_URL not in body:
            return response
        self.stripped = True
        logger.warning("EasyAuth manifest 拒绝 base_url,改为不带地址重试")
        return self._http.request("POST", _MANIFEST_PATH, json={_BODY_MANIFEST: manifest})


def _exchange(http: EasyAuthHttp, prepared: _Prepared) -> tuple[httpx.Response, int]:
    poster = _Poster(http, prepared.target.public_base_url)
    response = poster.send(prepared.manifest)
    if response.status_code != 409:
        return response, prepared.schema_version
    return _resolve_conflict(poster, prepared, response)


def _resolve_conflict(
    poster: _Poster,
    prepared: _Prepared,
    response: httpx.Response,
) -> tuple[httpx.Response, int]:
    """先探测已导入版本 N;只有这次仍 409 才推 N+1。开场版本已是 N 时不再重复探测。"""

    latest = _downstream_version(response)
    if latest is None or latest == prepared.schema_version:
        bumped = prepared.schema_version + 1 if latest is None else latest + 1
        logger.warning("EasyAuth manifest 版本冲突,改用 schema_version=%s 重试一次", bumped)
        return poster.send(_with_version(prepared.manifest, bumped)), bumped
    logger.warning("EasyAuth manifest 版本冲突,先探测 schema_version=%s", latest)
    probed = poster.send(_with_version(prepared.manifest, latest))
    if probed.status_code != 409:
        return probed, latest
    bumped = latest + 1
    logger.warning("EasyAuth manifest 探测仍冲突,改用 schema_version=%s", bumped)
    return poster.send(_with_version(prepared.manifest, bumped)), bumped


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
    if parsed is None:
        parsed = _parse_imported_version(response.text)
    if parsed is None or parsed < 1:
        return None
    return parsed


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
        return _Attempt("retryable", version, http_status=status, retry_after=_retry_after_seconds(response))
    return _Attempt("failed", version, http_status=status)


def _retry_after_seconds(response: httpx.Response) -> int:
    parsed = parse_retry_after_seconds(response)
    if parsed is None:
        return _RETRY_AFTER_FALLBACK_SECONDS
    return parsed


def _attempt_from_success(response: httpx.Response, version: int) -> _Attempt:
    try:
        payload = response_object(response)
    except EasyAuthProtocolError as exc:
        logger.warning("EasyAuth manifest 响应无效: %s", exc)
        return _Attempt("retryable", version, retry_after=_RETRY_AFTER_FALLBACK_SECONDS)
    outcome = _outcome(payload)
    if outcome is None:
        logger.warning("EasyAuth manifest 响应字段无效")
        return _Attempt("retryable", version, retry_after=_RETRY_AFTER_FALLBACK_SECONDS)
    already, template_version, catalog_version = outcome
    stored_version = _version_from_success(already, template_version, version)
    return _Attempt("ok", stored_version, already, template_version, catalog_version)


def _version_from_success(already: bool, template_version: int, sent: int) -> int:
    if not already:
        return sent
    accepted = _positive_int(template_version)
    if accepted is None:
        return sent
    return accepted


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


def _commit(
    store: ManifestSyncStore,
    previous: dict[str, Any] | None,
    prepared: _Prepared,
    attempt: _Attempt,
) -> None:
    if attempt.kind == "ok":
        store.save_state(_ok_payload(prepared, attempt))
        _log_ok(attempt)
        return
    if attempt.kind == "retryable":
        _defer(attempt.retry_after)
        return
    if attempt.http_status == _UNPROCESSABLE:
        store.save_state(_failure_422(previous, prepared))


def _defer(delay: int | None) -> None:
    if delay is None:
        return
    callback = _DEFER.get()
    if callback is None:
        return
    callback(delay)


def _ok_payload(prepared: _Prepared, attempt: _Attempt) -> dict[str, Any]:
    return {
        _HASH: prepared.content_hash,
        _VERSION: attempt.schema_version,
        _STATUS: _OK,
        _BASE_URL: prepared.base_url,
        _APP_KEY: prepared.app_key,
        _TEMPLATE: attempt.template_version,
        _CATALOG: attempt.catalog_version,
        _SYNCED_AT: _now(),
    }


def _failure_422(previous: dict[str, Any] | None, prepared: _Prepared) -> dict[str, Any]:
    """记下这次 422,但不把被拒版本写成下一次的地板。"""

    if _accepted_version(previous) is not None and previous is not None:
        payload = dict(previous)
        payload[_REJECTED_HASH] = prepared.content_hash
        payload[_REJECTED_BASE] = prepared.base_url
        payload[_REJECTED_APP] = prepared.app_key
    else:
        payload = {
            _HASH: prepared.content_hash,
            _STATUS: _FAILED,
            _BASE_URL: prepared.base_url,
            _APP_KEY: prepared.app_key,
        }
    payload[_HTTP_STATUS] = _UNPROCESSABLE
    payload[_SYNCED_AT] = _now()
    return payload


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
    return ManifestSyncResult(
        skipped=True,
        pushed=False,
        content_hash=digest,
        schema_version=_accepted_version(state),
    )


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
