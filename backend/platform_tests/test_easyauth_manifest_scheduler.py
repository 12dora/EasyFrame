"""ManifestSyncScheduler:单飞、force 合并、异常不逃出调度。"""

from __future__ import annotations

import threading

import pytest

from enterprise_platform.easyauth import ManifestSyncScheduler


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

    monkeypatch.setattr("enterprise_platform.easyauth.manifest_sync.threading.Thread", _Boom)
    scheduler = ManifestSyncScheduler(lambda force: None)
    scheduler.schedule(force=True)
    assert scheduler._running is False
