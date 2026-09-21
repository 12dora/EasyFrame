"""save_preference must wait on the policy row lock, then reject a just-enabled managed group."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import event, text

from blank_app.adapter_notification_settings import BlankNotificationSettingsAdapter
from blank_app.database import SessionLocal, engine
from blank_app.models import PlatformAuditLog, PlatformNotificationPolicy, PlatformNotificationPreference
from enterprise_platform.notification_settings import (
    CHANNEL_IN_APP,
    NotificationGroupManagedError,
    PolicyChange,
    SwitchChange,
)
from platform_tests.test_blank_notification_settings import GROUP, PREF_AUDIT, SCENE, _admin_id, _wipe_rows

pytestmark = pytest.mark.usefixtures("blank_admin_seeded")

_EVENT_TIMEOUT = 5.0
_JOIN_TIMEOUT = 8.0
_SET_LOCK_TIMEOUT = "SET LOCAL lock_timeout = '8s'"
_SET_STATEMENT_TIMEOUT = "SET LOCAL statement_timeout = '8s'"
_BLOCKED_SQL = text("SELECT CAST(:blocker AS integer) = ANY (pg_blocking_pids(CAST(:waiter AS integer)))")


@pytest.fixture(autouse=True)
def _clean_notification_rows():
    _wipe_rows()
    yield
    _wipe_rows()


def _wait(flag: threading.Event, label: str, *, timeout: float = _EVENT_TIMEOUT) -> None:
    assert flag.wait(timeout=timeout), label


def _is_policy_for_update(statement: object) -> bool:
    sql = str(statement).lower()
    return "platform_notification_policies" in sql and "for update" in sql


def _join_started(started: list[threading.Thread]) -> None:
    for thread in started:
        thread.join(timeout=_JOIN_TIMEOUT)
    alive = [thread.name for thread in started if thread.is_alive()]
    assert not alive, f"workers did not terminate: {alive}"


def _first_blocks_second(blocker: int, waiter: int) -> bool:
    with SessionLocal() as db:
        return bool(db.execute(_BLOCKED_SQL, {"blocker": blocker, "waiter": waiter}).scalar())


def _wait_for_policy_lock(race: _PolicyLockRace) -> None:
    deadline = time.monotonic() + _EVENT_TIMEOUT
    while time.monotonic() < deadline:
        blocker = race.pids.get(race.policy_thread)
        waiter = race.pids.get(race.pref_thread)
        if blocker is not None and waiter is not None and _first_blocks_second(blocker, waiter):
            return
        time.sleep(0.02)
    raise AssertionError("save_preference did not block on the in-flight save_policy row lock")


def _seed_unmanaged_policy() -> None:
    with SessionLocal() as db:
        db.add(PlatformNotificationPolicy(group_key=GROUP, managed=False, switches={}))
        db.commit()


def _assert_pref_rejected(race: _PolicyLockRace, account_id: str) -> None:
    assert race.policy_error is None
    assert race.pref_waited_for_release is True
    error = race.pref_error
    assert isinstance(error, NotificationGroupManagedError)
    assert error.group_key == GROUP
    parsed = uuid.UUID(account_id)
    with SessionLocal() as db:
        policy = db.get(PlatformNotificationPolicy, GROUP)
        assert policy is not None
        assert policy.managed is True
        assert db.get(PlatformNotificationPreference, (parsed, GROUP)) is None
        assert db.query(PlatformAuditLog).filter(PlatformAuditLog.action == PREF_AUDIT).first() is None


class _PolicyLockRace:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.adapter = BlankNotificationSettingsAdapter()
        self.policy_locked = threading.Event()
        self.policy_may_commit = threading.Event()
        self.pref_select_started = threading.Event()
        self.pref_waited_for_release = False
        self.policy_error: BaseException | None = None
        self.pref_error: BaseException | None = None
        self.pids: dict[threading.Thread, int] = {}
        self.started: list[threading.Thread] = []
        self.policy_thread = threading.Thread(target=self._run_policy, name="ns-policy", daemon=True)
        self.pref_thread = threading.Thread(target=self._run_pref, name="ns-pref", daemon=True)

    def _arm(self, cursor: Any, statement: object) -> None:
        current = threading.current_thread()
        if current not in (self.policy_thread, self.pref_thread):
            return
        if current in self.pids or not _is_policy_for_update(statement):
            return
        cursor.execute(_SET_LOCK_TIMEOUT)
        cursor.execute(_SET_STATEMENT_TIMEOUT)
        cursor.execute("SELECT pg_backend_pid()")
        self.pids[current] = int(cursor.fetchone()[0])

    def before_execute(self, _conn, cursor, statement, _parameters, _context, _executemany) -> None:
        self._arm(cursor, statement)
        if threading.current_thread() is self.pref_thread and _is_policy_for_update(statement):
            self.pref_select_started.set()

    def after_execute(self, _conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if not _is_policy_for_update(statement):
            return
        current = threading.current_thread()
        if current is self.policy_thread:
            self.policy_locked.set()
            _wait(self.policy_may_commit, "policy_may_commit", timeout=_JOIN_TIMEOUT)
        elif current is self.pref_thread:
            self.pref_waited_for_release = self.policy_may_commit.is_set()

    def _start(self, thread: threading.Thread) -> None:
        thread.start()
        self.started.append(thread)

    def run(self) -> None:
        self._start(self.policy_thread)
        _wait(self.policy_locked, "policy_locked")
        self._start(self.pref_thread)
        _wait(self.pref_select_started, "pref_select_started")
        _wait_for_policy_lock(self)
        self.policy_may_commit.set()
        _join_started(self.started)

    def _run_policy(self) -> None:
        try:
            self.adapter.save_policy(GROUP, PolicyChange(managed=True), actor_id=self.account_id)
        except BaseException as exc:
            self.policy_error = exc

    def _run_pref(self) -> None:
        try:
            _wait(self.policy_locked, "policy_locked")
            change = SwitchChange(scene=SCENE, channel=CHANNEL_IN_APP, enabled=False)
            self.adapter.save_preference(self.account_id, GROUP, change)
        except BaseException as exc:
            self.pref_error = exc


def test_save_preference_blocks_on_save_policy_then_raises_managed() -> None:
    account_id = _admin_id()
    _seed_unmanaged_policy()
    race = _PolicyLockRace(account_id)
    event.listen(engine, "before_cursor_execute", race.before_execute)
    event.listen(engine, "after_cursor_execute", race.after_execute)
    try:
        try:
            race.run()
            _assert_pref_rejected(race, account_id)
        finally:
            race.policy_may_commit.set()
            _join_started(race.started)
    finally:
        try:
            event.remove(engine, "before_cursor_execute", race.before_execute)
            event.remove(engine, "after_cursor_execute", race.after_execute)
        finally:
            _wipe_rows()
