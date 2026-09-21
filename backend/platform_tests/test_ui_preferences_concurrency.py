"""Concurrent ui_preferences patches must not drop independent keys."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import event, text

from blank_app.adapter_account import BlankAccountAdapter
from blank_app.database import SessionLocal, engine
from blank_app.models import Account

pytestmark = pytest.mark.usefixtures("blank_admin_seeded")

_KEEP_KEY = "keep_me"
_EVENT_TIMEOUT = 5.0
_JOIN_TIMEOUT = 8.0
_SET_LOCK_TIMEOUT = "SET LOCAL lock_timeout = '8s'"
_SET_STATEMENT_TIMEOUT = "SET LOCAL statement_timeout = '8s'"
_BLOCKED_SQL = text("SELECT CAST(:blocker AS integer) = ANY (pg_blocking_pids(CAST(:waiter AS integer)))")


def _wait(flag: threading.Event, label: str, *, timeout: float = _EVENT_TIMEOUT) -> None:
    assert flag.wait(timeout=timeout), label


def _is_account_for_update(statement: object) -> bool:
    sql = str(statement).lower()
    return "platform_accounts" in sql and "for update" in sql


def _insert_account() -> str:
    with SessionLocal() as db:
        account = Account(
            username=f"prefs-race-{uuid.uuid4().hex}",
            active=True,
            is_admin=False,
            must_change_password=False,
            ui_preferences={_KEEP_KEY: "yes"},
        )
        db.add(account)
        db.commit()
        return str(account.id)


def _delete_account(account_id: str) -> None:
    with SessionLocal() as db:
        db.execute(text("SET LOCAL lock_timeout = '3s'"))
        account = db.get(Account, account_id)
        if account is not None:
            db.delete(account)
            db.commit()


def _load_prefs(account_id: str) -> dict[str, Any]:
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        raw = account.ui_preferences if account is not None else {}
        return dict(raw) if isinstance(raw, dict) else {}


def _join_started(started: list[threading.Thread]) -> None:
    for thread in started:
        thread.join(timeout=_JOIN_TIMEOUT)
    alive = [thread.name for thread in started if thread.is_alive()]
    assert not alive, f"workers did not terminate: {alive}"


def _first_blocks_second(blocker: int, waiter: int) -> bool:
    with SessionLocal() as db:
        return bool(db.execute(_BLOCKED_SQL, {"blocker": blocker, "waiter": waiter}).scalar())


def _wait_for_row_lock(race: _PreferenceRace) -> None:
    deadline = time.monotonic() + _EVENT_TIMEOUT
    while time.monotonic() < deadline:
        blocker = race.pids.get(race.first_thread)
        waiter = race.pids.get(race.second_thread)
        if blocker is not None and waiter is not None and _first_blocks_second(blocker, waiter):
            return
        time.sleep(0.02)
    raise AssertionError("second update did not block on the first worker's row lock")


def _assert_merged(race: _PreferenceRace, account_id: str) -> None:
    assert race.errors == []
    assert race.second_saw_release is True
    stored = _load_prefs(account_id)
    assert stored == {
        _KEEP_KEY: "yes",
        "table_density": "comfortable",
        "row_spacing": "comfortable",
    }
    spacing = race.results["spacing"]
    assert spacing.get("table_density") == "comfortable"
    assert spacing.get("row_spacing") == "comfortable"


class _PreferenceRace:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.adapter = BlankAccountAdapter()
        self.first_read = threading.Event()
        self.first_may_commit = threading.Event()
        self.second_select_started = threading.Event()
        self.second_saw_release = False
        self.results: dict[str, dict[str, Any]] = {}
        self.errors: list[BaseException] = []
        self.pids: dict[threading.Thread, int] = {}
        self.started: list[threading.Thread] = []
        self.first_thread = threading.Thread(target=self._run_first, name="prefs-first", daemon=True)
        self.second_thread = threading.Thread(target=self._run_second, name="prefs-second", daemon=True)

    def _arm_worker(self, cursor: Any, statement: object) -> None:
        current = threading.current_thread()
        if current not in (self.first_thread, self.second_thread):
            return
        if current in self.pids or not _is_account_for_update(statement):
            return
        cursor.execute(_SET_LOCK_TIMEOUT)
        cursor.execute(_SET_STATEMENT_TIMEOUT)
        cursor.execute("SELECT pg_backend_pid()")
        self.pids[current] = int(cursor.fetchone()[0])

    def before_execute(self, _conn, cursor, statement, _parameters, _context, _executemany) -> None:
        self._arm_worker(cursor, statement)
        if threading.current_thread() is self.second_thread and _is_account_for_update(statement):
            self.second_select_started.set()

    def after_execute(self, _conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if not _is_account_for_update(statement):
            return
        current = threading.current_thread()
        if current is self.first_thread:
            self.first_read.set()
            _wait(self.first_may_commit, "first_may_commit", timeout=_JOIN_TIMEOUT)
        elif current is self.second_thread:
            self.second_saw_release = self.first_may_commit.is_set()

    def _start(self, thread: threading.Thread) -> None:
        thread.start()
        self.started.append(thread)

    def run(self) -> None:
        self._start(self.first_thread)
        _wait(self.first_read, "first_read")
        self._start(self.second_thread)
        _wait(self.second_select_started, "second_select_started")
        _wait_for_row_lock(self)
        self.first_may_commit.set()
        _join_started(self.started)

    def _run_first(self) -> None:
        try:
            self.results["density"] = self.adapter.update_ui_preferences(
                self.account_id, {"table_density": "comfortable"}
            )
        except BaseException as exc:
            self.errors.append(exc)

    def _run_second(self) -> None:
        try:
            _wait(self.first_read, "first_read")
            self.results["spacing"] = self.adapter.update_ui_preferences(
                self.account_id, {"row_spacing": "comfortable"}
            )
        except BaseException as exc:
            self.errors.append(exc)


def test_concurrent_preference_patches_keep_both_keys() -> None:
    """第二读必须发生在行锁释放之后,并看到第一档已写入的 JSON。"""

    account_id = _insert_account()
    race = _PreferenceRace(account_id)
    event.listen(engine, "before_cursor_execute", race.before_execute)
    event.listen(engine, "after_cursor_execute", race.after_execute)
    try:
        try:
            race.run()
            _assert_merged(race, account_id)
        finally:
            race.first_may_commit.set()
            _join_started(race.started)
    finally:
        try:
            event.remove(engine, "before_cursor_execute", race.before_execute)
            event.remove(engine, "after_cursor_execute", race.after_execute)
        finally:
            _delete_account(account_id)
