"""权限快照新鲜度分类与进程内后台刷新调度。"""

from __future__ import annotations

import logging
import threading
import weakref
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from enum import StrEnum

logger = logging.getLogger(__name__)

NEAR_EXPIRY_RATIO = 0.40
STALE_GRACE = timedelta(minutes=10)


class SnapshotFreshness(StrEnum):
    FRESH = "FRESH"
    NEAR_EXPIRY = "NEAR_EXPIRY"
    STALE_GRACE = "STALE_GRACE"
    EXPIRED = "EXPIRED"


def classify_snapshot(
    fetched_at: datetime,
    expires_at: datetime,
    now: datetime,
    *,
    near_expiry_ratio: float = NEAR_EXPIRY_RATIO,
    stale_grace: timedelta = STALE_GRACE,
) -> SnapshotFreshness:
    """按剩余寿命与宽限期分类。显式失效须 ``expires_at = fetched_at``,永不进宽限。

    naive datetime 视为 UTC。``expires_at <= fetched_at`` 一律 EXPIRED,
    即使 ``now < expires_at``(时钟回偏也不能把已吊销行当成 FRESH)。
    """

    fetched_at = _utc(fetched_at)
    expires_at = _utc(expires_at)
    now = _utc(now)
    if expires_at <= fetched_at:
        return SnapshotFreshness.EXPIRED
    if now < expires_at:
        return _freshness_before_expiry(fetched_at, expires_at, now, near_expiry_ratio)
    if now < expires_at + stale_grace:
        return SnapshotFreshness.STALE_GRACE
    return SnapshotFreshness.EXPIRED


def invalidated_expires_at(fetched_at: datetime) -> datetime:
    """显式失效(catalog.changed、管理员吊销等)把 ``expires_at`` 写成 ``fetched_at``。"""

    return fetched_at


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _freshness_before_expiry(
    fetched_at: datetime, expires_at: datetime, now: datetime, near_expiry_ratio: float
) -> SnapshotFreshness:
    remaining = expires_at - now
    window = expires_at - fetched_at
    if remaining < near_expiry_ratio * window:
        return SnapshotFreshness.NEAR_EXPIRY
    return SnapshotFreshness.FRESH


class BackgroundRefresher:
    """按 key 去重的后台刷新。完成的 Future 只留 WeakSet 给 ``wait``。"""

    def __init__(self, max_workers: int = 2, name: str = "authz-refresh") -> None:
        self._max_workers = max_workers
        self._name = name
        self._pool_lock = threading.Lock()
        self._pool: ThreadPoolExecutor | None = None
        self._shutdown = False
        self._inflight_lock = threading.Lock()
        self._inflight: set[str] = set()
        self._waiters_lock = threading.Lock()
        self._waiters: weakref.WeakSet[Future[object]] = weakref.WeakSet()

    def schedule(self, key: str, fn: Callable[[], object]) -> bool:
        if not self._claim_key(key):
            return False
        try:
            future = self._executor().submit(fn)
        except Exception:
            self._release_key(key)
            if self._shutdown:
                return False
            raise
        self._track(future, key)
        return True

    def wait(self, timeout: float = 5.0) -> None:
        with self._waiters_lock:
            pending = list(self._waiters)
        for future in pending:
            self._wait_one(future, timeout)

    def shutdown(self) -> None:
        with self._inflight_lock:
            self._shutdown = True
        with self._pool_lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def _claim_key(self, key: str) -> bool:
        with self._inflight_lock:
            if self._shutdown or key in self._inflight:
                return False
            self._inflight.add(key)
            return True

    def _release_key(self, key: str) -> None:
        with self._inflight_lock:
            self._inflight.discard(key)

    def _executor(self) -> ThreadPoolExecutor:
        with self._pool_lock:
            if self._shutdown:
                raise RuntimeError("background refresher is shut down")
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix=self._name)
            return self._pool

    def _track(self, future: Future[object], key: str) -> None:
        with self._waiters_lock:
            self._waiters.add(future)
        future.add_done_callback(lambda done, inflight_key=key: self._on_done(done, inflight_key))

    def _on_done(self, future: Future[object], key: str) -> None:
        self._release_key(key)
        with self._waiters_lock:
            self._waiters.discard(future)
        self._log_failure(future)

    def _log_failure(self, future: Future[object]) -> None:
        if future.cancelled():
            return
        error = future.exception()
        if error is None:
            return
        logger.error("authz background refresh failed", exc_info=error)

    def _wait_one(self, future: Future[object], timeout: float) -> None:
        try:
            future.result(timeout=timeout)
        except CancelledError:
            return
        except Exception:
            return
