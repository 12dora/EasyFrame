"""进程级短 TTL 盒子:单飞加载,generation 防止迟到写入覆盖失效后的值。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Generic, TypeVar

T = TypeVar("T")

_MISSING = object()


class TtlBox(Generic[T]):
    """``load`` 单飞且不持锁跑 loader;``set(..., generation=)`` 在 generation 变化时 no-op。"""

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._expires_at = 0.0
        self._value: T | object = _MISSING
        self._generation = 0
        self._inflight: Future[T] | None = None
        self._loader_thread: threading.Thread | None = None

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def peek(self) -> tuple[bool, T | None]:
        with self._lock:
            return self._peek_unlocked()

    def load(self, loader: Callable[[], T]) -> T:
        cached, inflight, generation, role = self._begin_load()
        if role == "hit":
            return cached  # type: ignore[return-value]
        if role == "wait":
            return inflight.result()  # type: ignore[union-attr]
        if role == "reenter":
            return self._load_reentrant(loader, generation)
        return self._load_leader(loader, generation, inflight)  # type: ignore[arg-type]

    def set(self, value: T, *, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._store_unlocked(value)

    def invalidate(self) -> None:
        with self._lock:
            self._value = _MISSING
            self._expires_at = 0.0
            self._generation += 1

    def _begin_load(self) -> tuple[T | None, Future[T] | None, int, str]:
        with self._lock:
            hit, cached = self._peek_unlocked()
            if hit:
                return cached, None, 0, "hit"
            if self._loader_thread is threading.current_thread():
                return None, None, self._generation, "reenter"
            if self._inflight is not None:
                return None, self._inflight, 0, "wait"
            future: Future[T] = Future()
            self._inflight = future
            self._loader_thread = threading.current_thread()
            return None, future, self._generation, "lead"

    def _load_reentrant(self, loader: Callable[[], T], generation: int) -> T:
        value = loader()
        self._store_if_generation(generation, value)
        return value

    def _load_leader(self, loader: Callable[[], T], generation: int, future: Future[T]) -> T:
        try:
            value = loader()
        except Exception as error:
            self._clear_inflight(future)
            future.set_exception(error)
            raise
        self._commit_load(generation, value, future)
        return value

    def _store_if_generation(self, generation: int, value: T) -> None:
        with self._lock:
            if generation == self._generation:
                self._store_unlocked(value)

    def _commit_load(self, generation: int, value: T, future: Future[T]) -> None:
        with self._lock:
            if generation == self._generation:
                self._store_unlocked(value)
            self._drop_inflight_unlocked(future)
        future.set_result(value)

    def _clear_inflight(self, future: Future[T]) -> None:
        with self._lock:
            self._drop_inflight_unlocked(future)

    def _drop_inflight_unlocked(self, future: Future[T]) -> None:
        if self._inflight is future:
            self._inflight = None
        if self._loader_thread is threading.current_thread():
            self._loader_thread = None

    def _peek_unlocked(self) -> tuple[bool, T | None]:
        if self._value is _MISSING or time.monotonic() >= self._expires_at:
            return False, None
        return True, self._value  # type: ignore[return-value]

    def _store_unlocked(self, value: T) -> None:
        self._value = value
        self._expires_at = time.monotonic() + self._ttl_seconds
