"""进程级短 TTL 盒子:单飞加载,generation 防止迟到写入覆盖失效后的值。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")

_MISSING = object()


class TtlBox(Generic[T]):
    """``load`` 在锁内单飞;``set(..., generation=)`` 在 generation 变化时 no-op。"""

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._expires_at = 0.0
        self._value: T | object = _MISSING
        self._generation = 0

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def peek(self) -> tuple[bool, T | None]:
        with self._lock:
            return self._peek_unlocked()

    def load(self, loader: Callable[[], T]) -> T:
        with self._lock:
            hit, cached = self._peek_unlocked()
            if hit:
                return cached  # type: ignore[return-value]
            generation = self._generation
            value = loader()
            if generation == self._generation:
                self._store_unlocked(value)
            return value

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

    def _peek_unlocked(self) -> tuple[bool, T | None]:
        if self._value is _MISSING or time.monotonic() >= self._expires_at:
            return False, None
        return True, self._value  # type: ignore[return-value]

    def _store_unlocked(self, value: T) -> None:
        self._value = value
        self._expires_at = time.monotonic() + self._ttl_seconds
