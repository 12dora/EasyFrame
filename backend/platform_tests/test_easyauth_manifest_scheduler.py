"""ManifestSyncScheduler:单飞、force 合并、异常不逃出调度。"""

from __future__ import annotations

import json
import threading
from typing import Any

import httpx
import pytest

from enterprise_platform.easyauth import ManifestSyncScheduler, sync_manifest
from platform_tests.test_easyauth_manifest_sync import _manifest, _MemoryStore, _target


def test_scheduler_single_flight_runs_pending_force() -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls: list[bool] = []
    active = {"n": 0, "peak": 0}

    def run(force: bool) -> None:
        active["n"] += 1
        active["peak"] = max(active["peak"], active["n"])
        calls.append(force)
        try:
            if len(calls) == 1:
                entered.set()
                release.wait(timeout=2)
            else:
                finished.set()
        finally:
            active["n"] -= 1

    scheduler = ManifestSyncScheduler(run)
    scheduler.schedule(force=False)
    assert entered.wait(timeout=2)
    scheduler.schedule(force=True)
    scheduler.schedule(force=False)
    release.set()
    assert finished.wait(timeout=2)
    assert calls == [False, True]
    assert active["peak"] == 1


def test_scheduler_runs_again_after_run_error() -> None:
    calls: list[bool] = []
    first_done = threading.Event()
    done = threading.Event()

    def run(force: bool) -> None:
        calls.append(force)
        if len(calls) == 1:
            first_done.set()
            raise RuntimeError("boom")
        done.set()

    scheduler = ManifestSyncScheduler(run)
    scheduler.schedule()
    assert first_done.wait(timeout=2)
    scheduler.schedule()
    assert done.wait(timeout=2)
    assert calls == [False, False]


def test_scheduler_thread_is_daemon() -> None:
    seen: list[threading.Thread] = []
    started = threading.Event()

    def run(force: bool) -> None:
        seen.append(threading.current_thread())
        started.set()

    ManifestSyncScheduler(run).schedule()
    assert started.wait(timeout=2)
    assert seen[0].daemon is True
    assert seen[0].name == "easyauth-manifest-sync"


def test_schedule_does_not_raise_when_thread_cannot_start(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def start(self) -> None:
            raise RuntimeError("no thread")

    monkeypatch.setattr("enterprise_platform.easyauth.manifest_scheduler.threading.Thread", _Boom)
    scheduler = ManifestSyncScheduler(lambda force: None)
    scheduler.schedule(force=True)
    assert scheduler._running is False


def test_failed_spawn_runs_coalesced_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    done = threading.Event()
    box: dict[str, ManifestSyncScheduler] = {}
    starts = {"n": 0}
    real_thread = threading.Thread

    class _Thread:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._args = args
            self._kwargs = kwargs

        def start(self) -> None:
            starts["n"] += 1
            if starts["n"] == 1:
                box["scheduler"]._pending = True
                box["scheduler"]._pending_force = True
                box["scheduler"]._pending_defer = False
                raise RuntimeError("no thread")
            real_thread(*self._args, **self._kwargs).start()

    def run(force: bool) -> None:
        calls.append(force)
        done.set()

    monkeypatch.setattr("enterprise_platform.easyauth.manifest_scheduler.threading.Thread", _Thread)
    scheduler = ManifestSyncScheduler(run)
    box["scheduler"] = scheduler
    scheduler.schedule(force=False)
    assert done.wait(timeout=2)
    assert calls == [True]


def test_worker_crash_respawns_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    done = threading.Event()
    original = ManifestSyncScheduler._take_pending
    seen = {"n": 0}

    def take(self: ManifestSyncScheduler) -> tuple[bool, bool] | None:
        seen["n"] += 1
        if seen["n"] == 1:
            with self._guard:
                self._pending = True
                self._pending_force = True
                self._pending_defer = False
            raise RuntimeError("boom")
        return original(self)

    def run(force: bool) -> None:
        calls.append(force)
        if force:
            done.set()

    monkeypatch.setattr(ManifestSyncScheduler, "_take_pending", take)
    ManifestSyncScheduler(run).schedule()
    assert done.wait(timeout=2)
    assert calls == [False, True]


def test_retryable_response_defers_once_using_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    armed = threading.Event()
    finished = threading.Event()
    timers: list[Any] = []
    posts: list[int] = []

    class _Timer:
        def __init__(
            self,
            interval: float,
            function: Any,
            args: tuple[object, ...] | None = None,
            kwargs: object = None,
        ) -> None:
            del kwargs
            self.interval = interval
            self.function = function
            self.args = args or ()
            self.daemon = False
            timers.append(self)

        def start(self) -> None:
            armed.set()

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        posts.append(1)
        return httpx.Response(
            429,
            headers={"Retry-After": "17"},
            json={"error": {"code": "THROTTLED", "message": "later", "details": {}}},
        )

    def run(force: bool) -> None:
        calls.append(force)
        sync_manifest(
            target=_target(),
            manifest=_manifest(),
            store=_MemoryStore(),
            transport=httpx.MockTransport(handler),
        )
        if len(calls) == 2:
            finished.set()

    monkeypatch.setattr("enterprise_platform.easyauth.manifest_scheduler.threading.Timer", _Timer)
    ManifestSyncScheduler(run).schedule()
    assert armed.wait(timeout=2)
    assert len(timers) == 1
    assert timers[0].interval == 17
    timers[0].function(*timers[0].args)
    assert finished.wait(timeout=2)
    assert calls == [False, False]
    assert posts == [1, 1]
    assert len(timers) == 1


def test_missing_retry_after_uses_sixty_second_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    armed = threading.Event()
    timers: list[float] = []

    class _Timer:
        def __init__(
            self,
            interval: float,
            function: object,
            args: object = None,
            kwargs: object = None,
        ) -> None:
            del function, args, kwargs
            timers.append(interval)
            self.daemon = False

        def start(self) -> None:
            armed.set()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        assert payload["manifest"]["schema_version"] == 1
        return httpx.Response(503, json={"error": {"code": "UNAVAILABLE", "message": "down", "details": {}}})

    def run(force: bool) -> None:
        del force
        sync_manifest(
            target=_target(),
            manifest=_manifest(),
            store=_MemoryStore(),
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("enterprise_platform.easyauth.manifest_scheduler.threading.Timer", _Timer)
    ManifestSyncScheduler(run).schedule()
    assert armed.wait(timeout=2)
    assert timers == [60]
