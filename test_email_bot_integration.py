import threading
import time
from email.message import EmailMessage
from types import SimpleNamespace

from f2_bootstrap import bootstrap_f2


bootstrap_f2()
from download_task_service import (  # noqa: E402
    DownloadExecutor,
    DownloaderRegistry,
    DownloadTaskService,
)
from download_types import SourceRef, TaskRequest, TaskStatus  # noqa: E402
from email_bot import EmailBot  # noqa: E402
from mail_state import MailStateStore  # noqa: E402
from task_store import TaskStore  # noqa: E402
from url_extractor import UrlExtractor  # noqa: E402


TEST_URL = "https://www.douyin.com/video/123"


class FakeImap:
    def __init__(self, raw):
        self.raw = raw
        self.stored = []

    def select(self, _mailbox):
        return "OK", [b"1"]

    def response(self, name):
        assert name == "UIDVALIDITY"
        return "OK", [b"77"]

    def uid(self, command, *args):
        if command == "search":
            return "OK", [b"9"]
        if command == "fetch":
            return "OK", [(b"9 (UID 9 BODY[] {0})", self.raw)]
        if command == "store":
            self.stored.append((args[0], args[2]))
            return "OK", [b"1"]
        raise AssertionError(command)

    def socket(self):
        return SimpleNamespace(shutdown=lambda _how: None, close=lambda: None)


class FakeDownloader:
    def __init__(self, *, path, delay=0, fail=False):
        self.path = path
        self.delay = delay
        self.fail = fail
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def matches(self, url):
        return url == TEST_URL

    def download(self, _url):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.fail:
                return {"success": False, "error": "permanent test failure"}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(b"fake media")
            return {
                "success": True,
                "filepath": str(self.path),
                "files": [str(self.path)],
                "file_count": 1,
                "title": "integration test",
            }
        finally:
            with self._lock:
                self.active -= 1


def make_raw_mail(*, url=TEST_URL, subject="下载"):
    message = EmailMessage()
    message["From"] = "sender@example.test"
    message["To"] = "bot@example.test"
    message["Subject"] = subject
    message["Message-ID"] = "<integration@example.test>"
    message.set_content(url)
    return message.as_bytes()


def make_bot(tmp_path, *, send_replies=False, worker_count=1, cooldown_seconds=0):
    bot = object.__new__(EmailBot)
    bot.config = SimpleNamespace(
        email=SimpleNamespace(
            email="bot@example.test",
            poll_interval=1,
            send_replies=send_replies,
        ),
        bot=SimpleNamespace(
            allowed_senders=[],
            subject_keyword="下载",
            cooldown_seconds=cooldown_seconds,
            transient_retry_attempts=1,
            transient_retry_delay_seconds=0,
            worker_count=worker_count,
            douyin_worker_count=worker_count,
            bilibili_worker_count=1,
            lease_seconds=30,
            heartbeat_seconds=1,
            outbox_retry_attempts=1,
            outbox_retry_delay_seconds=1,
        ),
    )
    bot._state = MailStateStore(tmp_path / "state.sqlite", default_lease_seconds=30)
    bot._task_service = None
    bot.extractor = UrlExtractor()
    bot._cooldowns = {}
    bot._sender_locks = {}
    bot._sender_locks_guard = threading.Lock()
    bot._held_sender_locks = {}
    bot._failure_file_lock = threading.Lock()
    bot._stop_event = threading.Event()
    bot._runtime_threads = []
    bot._pending_retries = {}
    bot._pending_retry_lock = threading.RLock()
    bot._legacy_cleanup_pending = set()
    bot._pending_retry_file = tmp_path / "pending.json"
    bot._failed_links_file = tmp_path / "failed.txt"
    bot._remove_legacy_retry = lambda _payload: None
    return bot


def install_service(bot, downloader, *, worker_count=1, max_attempts=1):
    registry = DownloaderRegistry()
    registry.register("douyin", downloader)
    service = DownloadTaskService(
        TaskStore(bot._state),
        DownloadExecutor(registry),
        worker_count=worker_count,
        platform_worker_counts={"douyin": worker_count},
        max_attempts=max_attempts,
        heartbeat_seconds=1,
        retry_delay_seconds=0,
        before_execute=bot._before_task_service_execute,
        after_execute=bot._after_task_service_execute,
        on_execute_finished=bot._after_task_service_finished,
    )
    bot._task_service = service
    return service


def stop_bot(bot):
    bot._stop_event.set()
    bot._task_service.shutdown(timeout=2)
    for thread in bot._runtime_threads:
        thread.join(timeout=2)
    assert not any(thread.is_alive() for thread in bot._runtime_threads)
    bot._state.close()


def wait_for_status(bot, task_id, status):
    deadline = time.time() + 3
    while time.time() < deadline:
        row = bot._state.get_task_by_id(task_id)
        if row and row["status"] == status:
            return row
        time.sleep(0.01)
    return bot._state.get_task_by_id(task_id)


def test_production_durable_mail_chain_suppresses_replies_but_completes(tmp_path):
    bot = make_bot(tmp_path, send_replies=False)
    downloader = FakeDownloader(path=tmp_path / "downloads" / "video.mp4")
    service = install_service(bot, downloader)
    mail = FakeImap(make_raw_mail())
    bot._imap_connect = lambda _cfg: mail

    bot._poll_once_durable(bot.config.email, bot.config.bot)
    task = bot._state.get_task("INBOX:77:9", TEST_URL)
    assert task is not None
    bot._start_runtime()
    try:
        row = wait_for_status(bot, task["id"], "succeeded")

        assert row is not None
        assert row["status"] == "succeeded"
        assert (tmp_path / "downloads" / "video.mp4").exists()
        assert bot._state._conn.execute("SELECT COUNT(*) FROM smtp_outbox").fetchone()[0] == 0
        deadline = time.time() + 3
        while time.time() < deadline and bot._state.list_task_events(consumer="email"):
            time.sleep(0.01)
        assert bot._state.list_task_events(consumer="email") == []
        assert not any(thread.name == "mail-smtp-outbox" for thread in bot._runtime_threads)
        assert mail.stored == [("9", "\\Seen")]
    finally:
        stop_bot(bot)


def test_terminal_failure_is_recorded_once_by_real_task_service(tmp_path):
    bot = make_bot(tmp_path, send_replies=False)
    downloader = FakeDownloader(path=tmp_path / "unused.mp4", fail=True)
    service = install_service(bot, downloader)
    submitted = service.submit(
        TaskRequest(TEST_URL, SourceRef("mail", "failure-1"), {"sender": "sender@example.test"})
    )
    bot._start_runtime()
    try:
        row = wait_for_status(bot, submitted.task_id, "failed")

        assert row is not None
        assert row["status"] == "failed"
        deadline = time.time() + 3
        while time.time() < deadline and not bot._failed_links_file.exists():
            time.sleep(0.01)
        failure_text = bot._failed_links_file.read_text(encoding="utf-8")
        assert failure_text.count(TEST_URL) == 1
    finally:
        stop_bot(bot)


def test_sender_cooldown_lock_serializes_same_sender_downloads(tmp_path):
    bot = make_bot(tmp_path, worker_count=2, cooldown_seconds=0)
    downloader = FakeDownloader(path=tmp_path / "downloads" / "video.mp4", delay=0.1)
    service = install_service(bot, downloader, worker_count=2)
    first = service.submit(
        TaskRequest(TEST_URL, SourceRef("mail", "serial-1"), {"sender": "sender@example.test"})
    )
    second = service.submit(
        TaskRequest(TEST_URL, SourceRef("mail", "serial-2"), {"sender": "sender@example.test"})
    )
    service.start()
    try:
        assert wait_for_status(bot, first.task_id, "succeeded") is not None
        assert wait_for_status(bot, second.task_id, "succeeded") is not None
        assert downloader.max_active == 1
        assert service.get(first.task_id).status == TaskStatus.SUCCEEDED
        assert service.get(second.task_id).status == TaskStatus.SUCCEEDED
    finally:
        stop_bot(bot)


def test_sender_lock_is_released_when_executor_raises(tmp_path):
    bot = make_bot(tmp_path, worker_count=1, cooldown_seconds=0)
    downloader = FakeDownloader(path=tmp_path / "downloads" / "video.mp4")
    service = install_service(bot, downloader, max_attempts=1)
    original_execute = service.executor.execute
    first = True

    def execute_once_then_succeed(url):
        nonlocal first
        if first:
            first = False
            raise RuntimeError("executor exploded")
        return original_execute(url)

    service.executor.execute = execute_once_then_succeed
    failed = service.submit(
        TaskRequest(TEST_URL, SourceRef("mail", "raise-1"), {"sender": "sender@example.test"})
    )
    service.start()
    try:
        assert wait_for_status(bot, failed.task_id, "failed") is not None
        assert bot._held_sender_locks == {}

        recovered = service.submit(
            TaskRequest(TEST_URL, SourceRef("mail", "raise-2"), {"sender": "sender@example.test"})
        )
        assert wait_for_status(bot, recovered.task_id, "succeeded") is not None
        assert bot._held_sender_locks == {}
    finally:
        stop_bot(bot)
