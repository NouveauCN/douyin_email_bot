import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from f2_bootstrap import bootstrap_f2


bootstrap_f2()
import email_bot as email_bot_module
from email_bot import EmailBot
from settings_store import SettingsStore


class _Gate:
    """A lock-shaped probe used to assert the claim/active critical section."""

    def __init__(self):
        self.held = False

    def __enter__(self):
        self.held = True
        return self

    def __exit__(self, *_exc):
        self.held = False


class _ActiveLock:
    def __init__(self, gate):
        self._gate = gate
        self._lock = threading.Lock()
        self.observed_gate = []

    def __enter__(self):
        self.observed_gate.append(self._gate.held)
        self._lock.acquire()
        return self

    def __exit__(self, *_exc):
        self._lock.release()


class _ClaimState:
    def __init__(self):
        self.task_claims = 0
        self.outbox_claims = 0

    def claim_tasks(self, *_args, **_kwargs):
        self.task_claims += 1
        if self.task_claims == 1:
            return [{"id": 1}]
        return []

    def claim_outbox(self, *_args, **_kwargs):
        self.outbox_claims += 1
        if self.outbox_claims == 1:
            return [{"id": 1}]
        return []


def _worker_bot(state, gate, active_lock):
    bot = object.__new__(EmailBot)
    bot._state = state
    bot._claim_gate = gate
    bot._active_lock = active_lock
    bot._active_tasks = 0
    bot._active_outbox = 0
    bot._intake_enabled = threading.Event()
    bot._intake_enabled.set()
    bot._stop_event = threading.Event()
    bot.config = SimpleNamespace(
        bot=SimpleNamespace(lease_seconds=30),
    )
    return bot


@pytest.mark.parametrize(
    ("loop_name", "active_name", "process_name"),
    [
        ("_task_worker_loop", "_active_tasks", "_process_durable_task"),
        ("_outbox_worker_loop", "_active_outbox", "_deliver_outbox"),
    ],
)
def test_claimed_work_is_counted_before_claim_gate_releases(
    loop_name, active_name, process_name
):
    state = _ClaimState()
    gate = _Gate()
    active_lock = _ActiveLock(gate)
    bot = _worker_bot(state, gate, active_lock)
    setattr(bot, process_name, lambda _item: bot._stop_event.set())

    worker = threading.Thread(
        target=getattr(bot, loop_name),
        args=("worker",) if loop_name == "_task_worker_loop" else (),
    )
    worker.start()
    worker.join(timeout=2)

    assert not worker.is_alive()
    # The first active-lock acquisition is the increment performed after the
    # lease claim.  The second is the matching decrement after processing and
    # is intentionally outside the claim gate.
    assert active_lock.observed_gate == [True, False]
    assert getattr(bot, active_name) == 0


def test_restart_gate_stops_new_task_and_outbox_claims():
    state = _ClaimState()
    gate = threading.Lock()
    bot = _worker_bot(state, gate, threading.Lock())
    bot._settings = SimpleNamespace(
        update_restart=lambda *_args, **_kwargs: None,
        heartbeat=lambda *_args, **_kwargs: None,
    )

    bot._begin_restart("request-1", 4)
    bot._task_worker_loop("worker")
    bot._outbox_worker_loop()

    assert state.task_claims == 0
    assert state.outbox_claims == 0
    assert not bot._intake_enabled.is_set()


class _LifecycleSettings:
    def __init__(self):
        self.updates = []
        self.heartbeats = []

    def update_restart(self, request_id, status, error=None, **details):
        self.updates.append((request_id, status, error, details))

    def heartbeat(self, *args, **kwargs):
        self.heartbeats.append((args, kwargs))


def _drain_bot(settings, *, active_tasks=0, active_outbox=0):
    bot = object.__new__(EmailBot)
    bot._settings = settings
    bot._restart_id = "request-1"
    bot._restart_started_at = time.monotonic()
    bot._restart_timeout = 300
    bot._active_lock = threading.Lock()
    bot._active_tasks = active_tasks
    bot._active_outbox = active_outbox
    bot._claim_gate = threading.Lock()
    bot._intake_enabled = threading.Event()
    bot._intake_enabled.set()
    bot._stop_event = threading.Event()
    bot._run_thread = None
    bot._run_finished = threading.Event()
    bot._run_finished.set()
    bot._runtime_threads = []
    bot._lifecycle_thread = None
    bot._lifecycle_stop = threading.Event()
    bot._boot_id = "boot-1"
    return bot


def test_restart_waits_for_inflight_work_then_marks_restarting():
    settings = _LifecycleSettings()
    bot = _drain_bot(settings, active_tasks=1)

    bot._finish_restart_if_ready()
    assert not bot._stop_event.is_set()
    assert settings.updates == []

    with bot._active_lock:
        bot._active_tasks = 0
    bot._finish_restart_if_ready()

    assert bot._stop_event.is_set()
    assert settings.updates[-1][1] == "restarting"
    assert settings.heartbeats[-1][1]["status"] == "restarting"


def test_restart_timeout_marks_forcing_and_uses_injected_exit():
    settings = _LifecycleSettings()
    exits = []
    bot = _drain_bot(settings, active_outbox=1)
    bot._restart_timeout = 1
    bot._restart_started_at = time.monotonic() - 2
    bot._exit_fn = exits.append

    bot._finish_restart_if_ready()

    assert exits == [75]
    assert not bot._stop_event.is_set()
    assert settings.updates[-1][1] == "forcing"
    assert settings.updates[-1][2] == "drain timeout"


def test_restart_waits_for_active_work_after_run_thread_has_exited():
    settings = _LifecycleSettings()
    bot = _drain_bot(settings, active_tasks=1)
    finished_thread = threading.Thread(target=lambda: None)
    finished_thread.start()
    finished_thread.join()
    bot._run_thread = finished_thread
    bot._run_finished = threading.Event()
    bot._run_finished.set()

    bot._restart_timeout = 10
    bot._finish_restart_if_ready()
    assert settings.updates == []
    assert not bot._stop_event.is_set()

    bot._restart_started_at = time.monotonic() - 11
    exits = []
    bot._exit_fn = exits.append
    bot._finish_restart_if_ready()
    assert exits == [75]
    assert settings.updates[-1][1] == "forcing"


def test_lifecycle_watcher_is_non_daemon_during_restart_drain(monkeypatch):
    created = {}

    class FakeThread:
        def __init__(self, **kwargs):
            created.update(kwargs)

        def start(self):
            created["started"] = True

    bot = object.__new__(EmailBot)
    bot._lifecycle_thread = None
    bot._lifecycle_stop = threading.Event()
    monkeypatch.setattr(email_bot_module.threading, "Thread", FakeThread)

    bot._start_lifecycle_watcher()

    assert created["daemon"] is False
    assert created["started"] is True


def test_shutdown_does_not_finalize_restart_without_durable_state():
    settings = _LifecycleSettings()
    bot = _drain_bot(settings)
    bot._state = None
    bot._runtime_threads = []
    bot._lifecycle_thread = None
    bot._lifecycle_stop = threading.Event()

    bot.shutdown()

    assert settings.updates == []
    assert settings.heartbeats == []
    assert not bot._lifecycle_stop.is_set()

    bot._finish_restart_if_ready()
    assert settings.updates[-1][1] == "restarting"
    assert settings.updates[-1][3]["active_count"] == 0
    assert settings.heartbeats[-1][1]["status"] == "restarting"
    assert bot._lifecycle_stop.is_set()


def test_blocked_imap_close_allows_shutdown_without_second_draining_write():
    settings = _LifecycleSettings()
    bot = _drain_bot(settings)
    bot._state = None
    close_started = threading.Event()
    release_close = threading.Event()

    def blocked_close():
        close_started.set()
        release_close.wait(timeout=2)

    bot._close_active_imap = blocked_close
    begin = threading.Thread(target=bot._begin_restart, args=("request-1", 4))
    begin.start()
    assert close_started.wait(timeout=1)

    bot.shutdown()
    assert [entry[1] for entry in settings.updates] == ["draining"]

    release_close.set()
    begin.join(timeout=2)
    assert not begin.is_alive()
    bot._run_finished.set()
    bot._finish_restart_if_ready()

    assert [entry[1] for entry in settings.updates] == ["draining", "restarting"]


class _CloseProbe:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class _JoinTimeoutWorker:
    def __init__(self):
        self.join_calls = []

    def join(self, timeout):
        self.join_calls.append(timeout)


def test_watcher_closes_state_after_shutdown_join_timeout_and_worker_finishes():
    settings = _LifecycleSettings()
    bot = _drain_bot(settings, active_tasks=1)
    bot._state = _CloseProbe()
    worker = _JoinTimeoutWorker()
    bot._runtime_threads = [worker]
    bot.shutdown()

    assert worker.join_calls == [30]
    assert bot._state.closed == 0
    assert settings.updates == []
    assert not bot._lifecycle_stop.is_set()

    with bot._active_lock:
        bot._active_tasks = 0
    bot._run_finished.set()
    bot._finish_restart_if_ready()

    assert bot._state.closed == 1
    assert settings.updates[-1][1] == "restarting"
    assert bot._lifecycle_stop.is_set()


def test_startup_marks_interrupted_restart_applied(tmp_path):
    store = SettingsStore(tmp_path / "settings.sqlite3")
    request_id = store.request_restart()
    store.update_restart(request_id, "restarting")
    bot = object.__new__(EmailBot)
    bot._settings = store
    bot._boot_id = "boot-2"
    bot._settings_revision = -1
    bot._managed_settings = {}

    bot._mark_startup()

    assert store.restart_status(request_id)["status"] == "applied"
    assert store.heartbeat_status()["status"] == "ok"


def test_cookie_reset_uses_effective_legacy_config_and_clear_stays_empty(tmp_path, monkeypatch):
    store = SettingsStore(tmp_path / "settings.sqlite3")
    (tmp_path / "config.yaml").write_text("douyin: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text("DOUYIN_COOKIE=legacy-cookie\n", encoding="utf-8")
    monkeypatch.setenv("RUNTIME_SETTINGS_DB", str(store.path))
    revision = store.apply_changes({"douyin.cookie": "old-cookie"})
    bot = object.__new__(EmailBot)
    bot._settings = store
    bot._project_dir = tmp_path
    bot._settings_revision = revision
    bot._managed_settings = store.managed_values()
    bot._cookie_lock = threading.Lock()
    bot.downloader = SimpleNamespace(config=SimpleNamespace(cookie="old-cookie"))

    reset_revision = store.reset(["douyin.cookie"], base_revision=revision)
    bot._handle_settings_revision(reset_revision)
    assert bot.downloader.config.cookie == "legacy-cookie"

    result = store.apply_and_maybe_restart(
        [{"key": "douyin.cookie", "action": "clear"}],
        base_revision=reset_revision,
    )
    bot._handle_settings_revision(result["revision"])

    assert result["request_id"] is None
    assert bot.downloader.config.cookie == ""
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM restart_requests").fetchone()[0] == 0


class _BlockingSocket:
    def __init__(self):
        self.closed = threading.Event()
        self.shutdown_calls = 0

    def shutdown(self, _how):
        self.shutdown_calls += 1
        self.closed.set()

    def close(self):
        self.closed.set()


class _BlockingImap:
    def __init__(self):
        self._socket = _BlockingSocket()

    def socket(self):
        return self._socket


def test_restart_closes_inflight_imap_socket_and_forces_stuck_run_thread():
    settings = _LifecycleSettings()
    bot = _drain_bot(settings)
    bot._run_finished = threading.Event()
    mail = _BlockingImap()
    bot._register_active_imap(mail)

    bot._begin_restart("request-1", 4)

    assert mail._socket.shutdown_calls == 1
    assert mail._socket.closed.is_set()
    assert bot._imap_connection is None

    stuck = threading.Thread(target=lambda: time.sleep(5), daemon=True)
    stuck.start()
    bot._run_thread = stuck
    bot._restart_timeout = 1
    bot._restart_started_at = time.monotonic() - 2
    exits = []
    bot._exit_fn = exits.append
    bot._finish_restart_if_ready()

    assert exits == [75]
    assert settings.updates[-1][1] == "forcing"


def test_startup_applies_restarting_and_forcing_only_loaded_revisions(tmp_path):
    store = SettingsStore(tmp_path / "settings.sqlite3")
    revision = store.apply_changes({"bot.subject_keyword": "download"})
    restarting = store.request_restart(revision=revision)
    forcing = store.request_restart(revision=revision)
    future = store.request_restart(revision=revision + 1)
    store.update_restart(restarting, "restarting")
    store.update_restart(forcing, "forcing")
    store.update_restart(future, "forcing")

    bot = object.__new__(EmailBot)
    bot._settings = store
    bot._boot_id = "boot-3"
    bot._settings_revision = -1
    bot._managed_settings = {}
    bot._mark_startup()

    assert store.restart_status(restarting)["status"] == "applied"
    assert store.restart_status(forcing)["status"] == "applied"
    assert store.restart_status(future)["status"] == "forcing"
