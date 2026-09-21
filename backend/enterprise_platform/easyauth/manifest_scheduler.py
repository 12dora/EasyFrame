"""进程内单飞的 manifest 推送调度。延迟重试通过 contextvar 交给同线程里的 ``sync_manifest``。"""

from __future__ import annotations

import contextvars
import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

_Defer = Callable[[int], None]
_DEFER: contextvars.ContextVar[_Defer | None] = contextvars.ContextVar("easyauth_manifest_defer", default=None)


class ManifestSyncScheduler:
    """进行中再次 ``schedule`` 只记一次补跑,``force`` 按或合并。"""

    def __init__(self, run: Callable[[bool], object], *, thread_name: str = "easyauth-manifest-sync") -> None:
        self._run = run
        self._thread_name = thread_name
        self._guard = threading.Lock()
        self._running = False
        self._pending = False
        self._pending_force = False
        self._pending_defer = False
        self._defer_armed = False

    def schedule(self, force: bool = False) -> None:
        """启动或并入补跑。永不向调用方抛错。"""

        try:
            self._start(force, defer=True)
        except Exception:
            logger.warning("EasyAuth manifest 同步调度失败", exc_info=True)

    def _start(self, force: bool, *, defer: bool) -> None:
        with self._guard:
            if self._running:
                self._pending = True
                self._pending_force = self._pending_force or force
                self._pending_defer = self._pending_defer or defer
                return
            self._running = True
        self._spawn(force, defer=defer)

    def _spawn(self, force: bool, *, defer: bool) -> None:
        pending: tuple[bool, bool] | None = (force, defer)
        for _ in range(4):
            if pending is None:
                return
            try:
                self._start_thread(pending[0], defer=pending[1])
            except Exception:
                logger.warning("EasyAuth manifest 同步线程未能启动", exc_info=True)
                pending = self._take_pending()
                continue
            return
        self._mark_stopped()

    def _start_thread(self, force: bool, *, defer: bool) -> None:
        threading.Thread(
            target=self._worker,
            args=(force, defer),
            name=self._thread_name,
            daemon=True,
        ).start()

    def _worker(self, force: bool, defer: bool) -> None:
        current = force
        allow_defer = defer
        try:
            while True:
                self._invoke(current, defer=allow_defer)
                claimed = self._take_pending()
                if claimed is None:
                    return
                current, allow_defer = claimed
        except Exception:
            logger.warning("EasyAuth manifest 同步线程失败", exc_info=True)
            self._respawn()

    def _invoke(self, force: bool, *, defer: bool) -> None:
        if not defer:
            self._call(force)
            return
        token = _DEFER.set(lambda seconds: self._arm_defer(seconds, force))
        try:
            self._call(force)
        finally:
            _DEFER.reset(token)

    def _call(self, force: bool) -> None:
        try:
            self._run(force)
        except Exception:
            logger.warning("EasyAuth manifest 同步失败", exc_info=True)

    def _arm_defer(self, seconds: int, force: bool) -> None:
        with self._guard:
            if self._defer_armed:
                return
            self._defer_armed = True
        try:
            timer = threading.Timer(seconds, self._fire_defer, args=(force,))
            timer.daemon = True
            timer.start()
        except Exception:
            self._clear_defer()
            raise
        logger.info("EasyAuth manifest 同步将在 %s 秒后重试一次", seconds)

    def _fire_defer(self, force: bool) -> None:
        self._clear_defer()
        try:
            self._start(force, defer=False)
        except Exception:
            logger.warning("EasyAuth manifest 延迟重试调度失败", exc_info=True)

    def _clear_defer(self) -> None:
        with self._guard:
            self._defer_armed = False

    def _respawn(self) -> None:
        claimed = self._take_pending()
        if claimed is None:
            return
        self._spawn(claimed[0], defer=claimed[1])

    def _take_pending(self) -> tuple[bool, bool] | None:
        with self._guard:
            if not self._pending:
                self._running = False
                return None
            force = self._pending_force
            defer = self._pending_defer
            self._pending = False
            self._pending_force = False
            self._pending_defer = False
            self._running = True
            return force, defer

    def _mark_stopped(self) -> None:
        with self._guard:
            self._running = False


__all__ = ["ManifestSyncScheduler"]
