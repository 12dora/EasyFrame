"""TtlBox:命中、单飞、generation 迟到写入不得覆盖。"""

from __future__ import annotations

import threading
import time

import pytest

from enterprise_platform.ttl_cache import TtlBox


def test_peek_miss_then_load_hit() -> None:
    box: TtlBox[str] = TtlBox(ttl_seconds=10)
    assert box.peek() == (False, None)
    assert box.load(lambda: "fresh") == "fresh"
    assert box.peek() == (True, "fresh")
    assert box.generation == 0


def test_late_set_does_not_clobber_newer_invalidate() -> None:
    box: TtlBox[str] = TtlBox(ttl_seconds=10)
    box.set("old", generation=box.generation)
    generation = box.generation
    box.invalidate()
    box.set("stale", generation=generation)
    assert box.peek() == (False, None)
    box.set("fresh", generation=box.generation)
    assert box.peek() == (True, "fresh")


def test_load_single_flight_on_miss() -> None:
    box: TtlBox[str] = TtlBox(ttl_seconds=10)
    calls = {"n": 0}
    entered = threading.Event()
    release = threading.Event()

    def slow() -> str:
        calls["n"] += 1
        entered.set()
        assert release.wait(timeout=5)
        return "once"

    results: list[str] = []

    def run() -> None:
        results.append(box.load(slow))

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    time.sleep(0.05)
    assert calls["n"] == 1
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert calls["n"] == 1
    assert results == ["once", "once"]


def test_expired_value_is_a_miss(monkeypatch) -> None:
    box: TtlBox[str] = TtlBox(ttl_seconds=1)
    clock = {"now": 100.0}
    monkeypatch.setattr("enterprise_platform.ttl_cache.time.monotonic", lambda: clock["now"])
    box.set("live", generation=box.generation)
    assert box.peek() == (True, "live")
    clock["now"] = 101.0
    assert box.peek() == (False, None)
    assert box.load(lambda: "reloaded") == "reloaded"


def test_loader_exception_is_not_cached() -> None:
    box: TtlBox[str] = TtlBox(ttl_seconds=10)

    def boom() -> str:
        raise RuntimeError("db blip")

    with pytest.raises(RuntimeError, match="db blip"):
        box.load(boom)
    assert box.peek() == (False, None)
    assert box.load(lambda: "ok") == "ok"


def test_reentrant_loader_does_not_deadlock() -> None:
    box: TtlBox[str] = TtlBox(ttl_seconds=10)

    def loader() -> str:
        hit, cached = box.peek()
        assert hit is False
        assert cached is None
        assert box.generation == 0
        return "ok"

    assert box.load(loader) == "ok"
    assert box.peek() == (True, "ok")


def test_invalidate_during_inflight_load_is_not_cached() -> None:
    box: TtlBox[str] = TtlBox(ttl_seconds=10)
    entered = threading.Event()
    release = threading.Event()

    def slow() -> str:
        entered.set()
        assert release.wait(timeout=5)
        return "stale"

    results: list[str] = []
    worker = threading.Thread(target=lambda: results.append(box.load(slow)))
    worker.start()
    assert entered.wait(timeout=5)
    box.invalidate()
    assert box.peek() == (False, None)
    release.set()
    worker.join(timeout=5)
    assert results == ["stale"]
    assert box.peek() == (False, None)
    assert box.load(lambda: "fresh") == "fresh"
    assert box.peek() == (True, "fresh")


def test_loader_exception_wakes_waiter_and_is_not_cached() -> None:
    box: TtlBox[str] = TtlBox(ttl_seconds=10)
    entered = threading.Event()
    release = threading.Event()

    def boom() -> str:
        entered.set()
        assert release.wait(timeout=5)
        raise RuntimeError("db blip")

    errors: list[BaseException] = []

    def leader() -> None:
        try:
            box.load(boom)
        except RuntimeError as error:
            errors.append(error)

    def waiter() -> None:
        try:
            box.load(lambda: "should-not-run")
        except RuntimeError as error:
            errors.append(error)

    first = threading.Thread(target=leader)
    second = threading.Thread(target=waiter)
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    time.sleep(0.05)
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert len(errors) == 2
    assert all(str(error) == "db blip" for error in errors)
    assert box.peek() == (False, None)
    assert box.load(lambda: "ok") == "ok"
    assert box.peek() == (True, "ok")
