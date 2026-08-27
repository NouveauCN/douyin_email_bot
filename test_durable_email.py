from email.message import EmailMessage
from types import SimpleNamespace

from f2_bootstrap import bootstrap_f2


bootstrap_f2()
from email_bot import EmailBot
from url_extractor import UrlExtractor
from mail_state import MailStateStore


class FakeSocket:
    def shutdown(self, _how):
        pass

    def close(self):
        pass


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
        raise AssertionError(command)

    def store(self, uid, _operation, flag):
        self.stored.append((uid, flag))
        return "OK", [b"1"]

    def socket(self):
        return FakeSocket()


def make_config(tmp_path):
    return SimpleNamespace(
        email=SimpleNamespace(email="bot@example.test", poll_interval=30),
        bot=SimpleNamespace(
            durable_mail_enabled=True,
            state_db=str(tmp_path / "state.sqlite"),
            transient_pending_file=str(tmp_path / "pending.json"),
            transient_failed_file=str(tmp_path / "failed.txt"),
            allowed_senders=[],
            subject_keyword="下载",
            cooldown_seconds=5,
            transient_retry_attempts=3,
            transient_retry_delay_seconds=10,
            worker_count=1,
            douyin_worker_count=1,
            bilibili_worker_count=1,
            lease_seconds=30,
            heartbeat_seconds=1,
            outbox_retry_attempts=3,
            outbox_retry_delay_seconds=1,
            commands=SimpleNamespace(cookie_update="更新cookie", cookie_auto="自动获取cookie"),
        ),
        douyin=SimpleNamespace(download_path=str(tmp_path / "downloads"), cookie=""),
        bilibili=SimpleNamespace(),
        media_cleanup=SimpleNamespace(backup_retention_days=28, check_interval_days=7),
        cookie_extractor=SimpleNamespace(profile_dir="", headless=True, validate=True),
    )


def make_raw_mail(url="https://www.douyin.com/video/123"):
    msg = EmailMessage()
    msg["From"] = "user@example.test"
    msg["To"] = "bot@example.test"
    msg["Subject"] = "下载"
    msg["Message-ID"] = "<mail-9@example.test>"
    msg.set_content(url)
    return msg.as_bytes()


def make_bot(tmp_path):
    bot = object.__new__(EmailBot)
    bot.config = make_config(tmp_path)
    bot._state = MailStateStore(bot.config.bot.state_db, default_lease_seconds=30)
    bot.extractor = UrlExtractor()
    bot._pending_retries = {}
    bot._cooldowns = {}
    bot._project_dir = tmp_path
    bot._cookie_lock = __import__("threading").Lock()
    bot._sender_locks = {}
    bot._sender_locks_guard = __import__("threading").Lock()
    bot.downloader = SimpleNamespace(config=SimpleNamespace(cookie=""))
    bot.bilibili_downloader = SimpleNamespace()
    bot._platform_locks = {"douyin": __import__("threading").BoundedSemaphore(1)}
    bot._remove_legacy_retry = lambda _payload: None
    return bot


def test_uid_intake_commits_before_seen_and_creates_one_task(tmp_path):
    bot = make_bot(tmp_path)
    mail = FakeImap(make_raw_mail())
    bot._imap_connect = lambda _cfg: mail

    bot._poll_once_durable(bot.config.email, bot.config.bot)

    task = bot._state._conn.execute(
        "SELECT normalized_url, status FROM tasks"
    ).fetchone()
    assert task["normalized_url"] == "https://www.douyin.com/video/123"
    assert task["status"] == "pending"
    assert mail.stored == [(b"9", "\\Seen")]
    assert bot._state.pending_seen("INBOX") == []
    bot._state.close()


def test_download_completion_and_smtp_delivery_are_recoverable(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    accepted = bot._state.accept_message(
        "INBOX", 77, 9, "INBOX:77:9", ["https://www.douyin.com/video/123"],
        metadata={"sender": "user@example.test", "subject": "下载"},
        platform="douyin",
    )
    task = bot._state.claim_tasks()[0]
    bot._download_url = lambda _url: {
        "success": True,
        "filepath": "/app/downloads/a.mp4",
        "title": "a",
        "files": ["/app/downloads/a.mp4"],
        "file_count": 1,
    }
    sent = []
    bot._send_reply = lambda *args, **kwargs: sent.append((args, kwargs))

    bot._process_durable_task(task)

    result = bot._state._conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (accepted["task_ids"][0],)
    ).fetchone()
    assert result["status"] == "succeeded"
    outbox = bot._state.claim_outbox()[0]
    bot._deliver_outbox(outbox)
    assert sent[0][1]["message_id"] == outbox["message_id"]
    assert bot._state._conn.execute(
        "SELECT status FROM smtp_outbox WHERE id = ?", (outbox["id"],)
    ).fetchone()["status"] == "sent"
    bot._state.close()


def test_seen_failure_remains_pending_for_next_reconciliation(tmp_path):
    bot = make_bot(tmp_path)
    mail = FakeImap(make_raw_mail())
    mail.store = lambda *_args: ("NO", [b"temporary failure"])
    bot._imap_connect = lambda _cfg: mail

    bot._poll_once_durable(bot.config.email, bot.config.bot)

    pending = bot._state.pending_seen("INBOX")
    assert len(pending) == 1
    assert pending[0]["uid"] == 9
    bot._state.close()


def test_partial_retry_exhaustion_is_terminal_and_not_silent(tmp_path):
    bot = make_bot(tmp_path)
    bot.config.bot.transient_retry_attempts = 1
    accepted = bot._state.accept_message(
        "INBOX", 77, 10, "INBOX:77:10", ["https://www.douyin.com/video/456"],
        metadata={"sender": "user@example.test", "subject": "下载"},
        platform="douyin",
    )
    task = bot._state.claim_tasks()[0]
    bot._download_url = lambda _url: {
        "success": True,
        "partial": True,
        "filepath": "/app/downloads/slides",
        "title": "partial",
        "file_count": 1,
        "files": ["/app/downloads/slides/one.webp"],
        "failed_items": ["two.webp: network timeout"],
    }
    bot._record_failed_link = lambda *_args: None

    bot._process_durable_task(task)

    assert bot._state._conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (accepted["task_ids"][0],)
    ).fetchone()["status"] == "failed"
    assert bot._state._conn.execute(
        "SELECT event FROM smtp_outbox WHERE task_id = ?", (accepted["task_ids"][0],)
    ).fetchone()["event"] == "partial-failed"
    bot._state.close()
