import sqlite3
from email import message_from_bytes
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from f2_bootstrap import bootstrap_f2


bootstrap_f2()
from email_bot import EmailBot, _decode_str, _get_body_text, _mark_seen_uid
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
        if command == "store":
            self.stored.append((args[0], args[2]))
            return "OK", [b"1"]
        raise AssertionError(command)

    def store(self, uid, _operation, flag):
        self.stored.append((uid, flag))
        return "OK", [b"1"]

    def socket(self):
        return FakeSocket()


class MultiUidImap(FakeImap):
    def __init__(self, raws):
        super().__init__(None)
        self.raws = raws

    def uid(self, command, *args):
        if command == "search":
            criteria = args[1]
            if criteria.startswith("UID "):
                start = int(criteria.split()[1].split(":", 1)[0])
                uids = [uid for uid in self.raws if uid >= start]
            else:
                uids = list(self.raws)
            return "OK", [" ".join(str(uid) for uid in uids).encode()]
        if command == "fetch":
            uid = int(args[0])
            return "OK", [
                (f"{uid} (UID {uid} BODY[] {{0}})".encode(), self.raws[uid])
            ]
        if command == "store":
            self.stored.append((args[0], args[2]))
            return "OK", [b"1"]
        raise AssertionError(command)


class ResetAwareImap(MultiUidImap):
    def __init__(self, raws, *, uidvalidity=77, all_uids=(), unseen_uids=(), fail_uids=()):
        super().__init__(raws)
        self.uidvalidity = uidvalidity
        self.all_uids = list(all_uids)
        self.unseen_uids = list(unseen_uids)
        self.fail_uids = set(fail_uids)
        self.searches = []
        self.fetches = []

    def response(self, _name):
        return "OK", [str(self.uidvalidity).encode()]

    def uid(self, command, *args):
        if command == "search":
            criteria = args[1]
            self.searches.append(criteria)
            if criteria == "ALL":
                uids = self.all_uids
            elif criteria == "UNSEEN":
                uids = self.unseen_uids
            else:
                start = int(criteria.split()[1].split(":", 1)[0])
                uids = [uid for uid in self.raws if uid >= start]
            return "OK", [" ".join(str(uid) for uid in uids).encode()]
        if command == "fetch":
            uid = int(args[0])
            self.fetches.append(uid)
            if uid in self.fail_uids:
                return "NO", []
            return "OK", [
                (f"{uid} (UID {uid} BODY[] {{0}})".encode(), self.raws[uid])
            ]
        if command == "store":
            self.stored.append((args[0], args[2]))
            return "OK", [b"1"]
        raise AssertionError(command)


class LegacyImap(FakeImap):
    def fetch(self, _msg_id, _query):
        return "OK", [(b"9 (RFC822 {0})", self.raw)]


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


def make_raw_mail(
    url="https://www.douyin.com/video/123",
    subject="下载",
    message_id="<mail-9@example.test>",
):
    msg = EmailMessage()
    msg["From"] = "user@example.test"
    msg["To"] = "bot@example.test"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg.set_content(url or "ordinary mail")
    return msg.as_bytes()


def make_raw_cookie_mail(body="sessionid=" + "s" * 128):
    msg = EmailMessage()
    msg["From"] = "user@example.test"
    msg["To"] = "bot@example.test"
    msg["Subject"] = "更新cookie"
    msg["Message-ID"] = "<cookie-12@example.test>"
    msg.set_content(body)
    return msg.as_bytes()


def make_legacy_orphan_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE source_messages (
            source_message_id TEXT PRIMARY KEY,
            mailbox TEXT NOT NULL,
            uidvalidity INTEGER NOT NULL,
            uid INTEGER NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            seen_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        INSERT INTO source_messages VALUES
            ('INBOX:77:12', 'INBOX', 77, 12, '{}', NULL, 1, 1);
        PRAGMA user_version = 1;
        """
    )
    conn.commit()
    conn.close()


def make_bot(tmp_path):
    bot = object.__new__(EmailBot)
    bot.config = make_config(tmp_path)
    bot._state = MailStateStore(bot.config.bot.state_db, default_lease_seconds=30)
    bot.extractor = UrlExtractor()
    bot._pending_retries = {}
    bot._pending_retry_lock = __import__("threading").RLock()
    bot._legacy_cleanup_pending = set()
    bot._pending_retry_file = tmp_path / "pending.json"
    bot._failed_links_file = tmp_path / "failed.txt"
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


def test_mail_text_decoding_handles_unknown_charset_and_multipart_subject():
    raw = (
        b"From: user@example.test\r\n"
        b"Subject: =?unknown-8bit?b?5L2g?= =?utf-8?b?5aW9?=\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=x-unknown\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n\xe4\xbd\xa0\xe5\xa5\xbd"
    )
    msg = message_from_bytes(raw)

    assert _decode_str(msg["Subject"]) == "你好"
    assert _get_body_text(msg) == "你好"


def test_legacy_email_skips_body_link_without_subject_keyword(tmp_path):
    bot = make_bot(tmp_path)
    bot._state.close()
    bot._state = None
    bot._seen_ids = set()
    mail = LegacyImap(make_raw_mail(subject="随便看看"))
    downloaded = []
    replies = []
    bot._download_url = lambda url: downloaded.append(url) or {
        "success": True,
        "filepath": "/tmp/video.mp4",
        "title": "video",
        "files": ["/tmp/video.mp4"],
        "file_count": 1,
    }
    bot._send_reply = lambda *args, **kwargs: replies.append((args, kwargs))

    bot._process_email(mail, b"9", bot.config.email, bot.config.bot)

    assert downloaded == []
    assert replies == []
    # Legacy keyword skips remain unseen and are retried on the next poll.
    assert mail.stored == []


def test_durable_email_skips_body_link_without_subject_keyword(tmp_path):
    bot = make_bot(tmp_path)
    mail = FakeImap(make_raw_mail(subject="随便看看"))
    bot._imap_connect = lambda _cfg: mail

    bot._poll_once_durable(bot.config.email, bot.config.bot)

    assert bot._state._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert bot._state._conn.execute("SELECT COUNT(*) FROM smtp_outbox").fetchone()[0] == 0
    bot._state.close()


def test_reset_baseline_does_not_fetch_historical_seen_mail(tmp_path):
    bot = make_bot(tmp_path)
    mail = ResetAwareImap(
        {1: make_raw_mail(message_id="<seen@example.test>"), 2: make_raw_mail()},
        all_uids=[1, 2],
        unseen_uids=[2],
    )
    bot._imap_connect = lambda _cfg: mail

    bot._poll_once_durable(bot.config.email, bot.config.bot)

    assert mail.searches == ["ALL", "UNSEEN"]
    assert mail.fetches == [2]
    assert bot._state.get_mailbox_position("INBOX")["last_uid"] == 2
    bot._state.close()


def test_uidvalidity_reset_routes_only_unseen_mail(tmp_path):
    bot = make_bot(tmp_path)
    bot._state.set_mailbox_position("INBOX", 76, 99)
    mail = ResetAwareImap(
        {1: make_raw_mail(message_id="<old-generation@example.test>"), 2: make_raw_mail()},
        uidvalidity=77,
        all_uids=[1, 2],
        unseen_uids=[2],
    )
    bot._imap_connect = lambda _cfg: mail

    bot._poll_once_durable(bot.config.email, bot.config.bot)

    assert mail.fetches == [2]
    position = bot._state.get_mailbox_position("INBOX")
    assert position["uidvalidity"] == 77
    assert position["last_uid"] == 2
    assert position["generation"] == 2
    bot._state.close()


def test_reset_with_all_seen_mail_only_commits_baseline(tmp_path):
    bot = make_bot(tmp_path)
    mail = ResetAwareImap(
        {1: make_raw_mail(message_id="<seen-one@example.test>"), 2: make_raw_mail(message_id="<seen-two@example.test>")},
        all_uids=[1, 2],
        unseen_uids=[],
    )
    bot._imap_connect = lambda _cfg: mail

    bot._poll_once_durable(bot.config.email, bot.config.bot)

    assert mail.fetches == []
    assert bot._state.get_mailbox_position("INBOX")["last_uid"] == 2
    assert bot._state._conn.execute("SELECT COUNT(*) FROM source_messages").fetchone()[0] == 0
    bot._state.close()


def test_reset_candidate_fetch_failure_does_not_commit_baseline(tmp_path):
    bot = make_bot(tmp_path)
    mail = ResetAwareImap(
        {2: make_raw_mail()}, all_uids=[2], unseen_uids=[2], fail_uids=[2]
    )
    bot._imap_connect = lambda _cfg: mail

    bot._poll_once_durable(bot.config.email, bot.config.bot)

    assert mail.fetches == [2]
    assert bot._state.get_mailbox_position("INBOX") is None
    bot._state.close()


def test_mail_arriving_after_reset_unseen_is_caught_by_next_uid_range(tmp_path):
    bot = make_bot(tmp_path)
    mail = ResetAwareImap({1: make_raw_mail(), 2: make_raw_mail()}, all_uids=[1], unseen_uids=[])
    bot._imap_connect = lambda _cfg: mail

    bot._poll_once_durable(bot.config.email, bot.config.bot)
    mail.all_uids = [1, 2]
    bot._poll_once_durable(bot.config.email, bot.config.bot)

    assert mail.fetches == [2]
    assert mail.searches == ["ALL", "UNSEEN", "UID 2:*", "UNSEEN"]
    assert bot._state.get_mailbox_position("INBOX")["last_uid"] == 2
    bot._state.close()


def test_no_link_email_is_seen_without_smtp_notice_legacy_or_durable(tmp_path):
    bot = make_bot(tmp_path)
    bot._state.close()
    bot._state = None
    bot._seen_ids = set()
    mail = LegacyImap(make_raw_mail(url=None, subject="普通通知"))
    replies = []
    bot._send_reply = lambda *args, **kwargs: replies.append((args, kwargs))

    bot._process_email(mail, b"9", bot.config.email, bot.config.bot)

    assert replies == []
    assert mail.stored == [(b"9", "\\Seen")]

    durable = make_bot(tmp_path / "durable")
    durable_mail = FakeImap(make_raw_mail(url=None, subject="普通通知"))
    durable._imap_connect = lambda _cfg: durable_mail
    durable._poll_once_durable(durable.config.email, durable.config.bot)

    assert durable_mail.stored == [("9", "\\Seen")]
    assert durable._state._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert durable._state._conn.execute("SELECT COUNT(*) FROM smtp_outbox").fetchone()[0] == 0
    bot._state = None
    durable._state.close()


def test_durable_routing_exception_is_quarantined_and_next_uid_continues(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    raw_bad = make_raw_mail(
        url="secret body", subject="下载 bad subject", message_id="<bad@example.test>"
    )
    raw_good = make_raw_mail(
        subject="下载主题", message_id="<good@example.test>"
    )
    mail = MultiUidImap({9: raw_bad, 10: raw_good})
    bot._imap_connect = lambda _cfg: mail

    def extract(text):
        if "secret body" in text:
            raise ValueError("secret subject/body detail")
        return "https://www.douyin.com/video/123"

    monkeypatch.setattr(bot.extractor, "extract", extract)
    bot._poll_once_durable(bot.config.email, bot.config.bot)

    quarantined = bot._state._conn.execute(
        "SELECT metadata_json, intake_complete, seen_at FROM source_messages "
        "WHERE source_message_id = ?",
        ("INBOX:77:9",),
    ).fetchone()
    assert quarantined["intake_complete"] == 1
    assert quarantined["seen_at"] is not None
    assert quarantined["metadata_json"] == (
        '{"intake_error": "ValueError", "raw_sha256": "'
        + __import__("hashlib").sha256(raw_bad).hexdigest()
        + '", "uid": 9, "uidvalidity": 77}'
    )
    assert "secret subject/body detail" not in quarantined["metadata_json"]
    assert mail.stored == [("9", "\\Seen"), ("10", "\\Seen")]
    assert bot._state._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    bot._state.close()


def test_repeated_durable_poll_does_not_duplicate_tasks_or_outbox(tmp_path):
    bot = make_bot(tmp_path)
    mail = FakeImap(make_raw_mail())
    bot._imap_connect = lambda _cfg: mail

    bot._poll_once_durable(bot.config.email, bot.config.bot)
    counts_before = [
        bot._state._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("tasks", "smtp_outbox")
    ]
    bot._poll_once_durable(bot.config.email, bot.config.bot)
    counts_after = [
        bot._state._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("tasks", "smtp_outbox")
    ]

    assert counts_before == counts_after == [1, 0]
    bot._state.close()


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
    assert mail.stored == [("9", "\\Seen")]
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
    mail.uid = lambda command, *_args: (
        ("NO", [b"temporary failure"])
        if command == "store"
        else ("OK", [b"9"])
        if command == "search"
        else ("OK", [(b"9 (UID 9 BODY[] {0})", mail.raw)])
    )
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


def test_terminal_legacy_retry_failure_removes_json_mirror(tmp_path):
    bot = make_bot(tmp_path)
    bot._remove_legacy_retry = EmailBot._remove_legacy_retry.__get__(bot, EmailBot)
    removed = []
    original_remove = bot._remove_legacy_retry
    bot._remove_legacy_retry = lambda payload: (
        removed.append(payload), original_remove(payload)
    )[1]
    bot.config.bot.transient_retry_attempts = 1
    bot._pending_retries = {
        "legacy-key": {
            "url": "https://www.douyin.com/video/789",
            "sender": "user@example.test",
            "attempts": 1,
        }
    }
    assert "legacy-key" in bot._pending_retries
    accepted = bot._state.accept_message(
        "INBOX", 77, 15, "INBOX:77:15", ["https://www.douyin.com/video/789"],
        metadata={"sender": "user@example.test", "legacy_retry_key": "legacy-key"},
        platform="douyin",
    )
    task = bot._state.claim_tasks()[0]
    assert task["payload"]["legacy_retry_key"] == "legacy-key"
    bot._download_url = lambda _url: {
        "success": False,
        "error": "permanent failure",
        "filepath": None,
        "files": [],
        "file_count": 0,
    }
    bot._record_failed_link = lambda *_args: None

    bot._process_durable_task(task)

    assert removed == [task["payload"]]
    assert removed[0]["legacy_retry_key"] == "legacy-key"
    assert "legacy-key" not in bot._pending_retries
    assert bot._state._conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (accepted["task_ids"][0],)
    ).fetchone()["status"] == "failed"
    bot._state.close()


def test_legacy_retry_removal_restores_entry_when_json_save_fails(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    bot._remove_legacy_retry = EmailBot._remove_legacy_retry.__get__(bot, EmailBot)
    bot._pending_retries = {"legacy-key": {"url": "https://example.test/a"}}
    monkeypatch.setattr(bot, "_save_pending_retries", lambda: False)

    assert bot._remove_legacy_retry({"legacy_retry_key": "legacy-key"}) is False
    assert "legacy-key" in bot._pending_retries
    assert "legacy-key" in bot._legacy_cleanup_pending


def test_concurrent_legacy_retry_removal_is_serialized(tmp_path):
    bot = make_bot(tmp_path)
    bot._remove_legacy_retry = EmailBot._remove_legacy_retry.__get__(bot, EmailBot)
    bot._pending_retries = {"legacy-key": {"url": "https://example.test/a"}}
    threads = [
        __import__("threading").Thread(
            target=bot._remove_legacy_retry,
            args=({"legacy_retry_key": "legacy-key"},),
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert "legacy-key" not in bot._pending_retries


def test_legacy_retry_is_kept_when_durable_failure_lost_its_lease(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    bot._remove_legacy_retry = EmailBot._remove_legacy_retry.__get__(bot, EmailBot)
    bot._pending_retries = {"legacy-key": {"url": "https://example.test/a"}}
    bot._record_failed_link = lambda *_args: None
    monkeypatch.setattr(bot._state, "fail_task", lambda *_args, **_kwargs: None)

    bot._complete_durable_failure(
        {"id": 1, "payload": {"legacy_retry_key": "legacy-key"}},
        "lost-lease",
        "permanent failure",
    )

    assert "legacy-key" in bot._pending_retries


def test_uid_fetch_never_falls_back_to_sequence_fetch():
    calls = []

    class UidUnavailable:
        def uid(self, *_args):
            raise AttributeError("UID command unavailable")

        def fetch(self, *_args):
            calls.append("sequence-fetch")
            return "OK", [(b"header", make_raw_mail())]

    assert EmailBot._imap_fetch_uid(UidUnavailable(), 9) is None
    assert calls == []


@pytest.mark.parametrize("metadata", [b"9 (BODY[] {10})", b"9 (UID 10 BODY[] {10})"])
def test_uid_fetch_rejects_missing_or_mismatched_uid(metadata):
    class WrongUid:
        def uid(self, command, *_args):
            assert command == "fetch"
            return "OK", [(metadata, make_raw_mail())]

    assert EmailBot._imap_fetch_uid(WrongUid(), 9) is None


def test_missing_uidvalidity_aborts_durable_intake(tmp_path):
    bot = make_bot(tmp_path)
    mail = FakeImap(make_raw_mail())

    class MissingUidValidity(FakeImap):
        def response(self, _name):
            return "OK", []

        def uid(self, *_args):
            raise AssertionError("durable intake must not search without UIDVALIDITY")

    mail = MissingUidValidity(mail.raw)
    bot._imap_connect = lambda _cfg: mail

    bot._poll_once_durable(bot.config.email, bot.config.bot)

    assert bot._state.get_mailbox_position("INBOX") is None
    assert bot._state._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    bot._state.close()


def test_legacy_incomplete_source_is_fetched_before_seen_ack(tmp_path):
    state_path = tmp_path / "state.sqlite"
    make_legacy_orphan_db(state_path)
    bot = make_bot(tmp_path)
    events = []

    class LegacyPendingIntakeMail(FakeImap):
        def response(self, _name):
            return "OK", [b"77"]

        def uid(self, command, *args):
            if command == "search":
                return "OK", [b""]
            if command == "fetch":
                events.append(("fetch", int(args[0])))
                return "OK", [(b"12 (UID 12 BODY[] {0})", self.raw)]
            if command == "store":
                events.append(("store", args[0]))
                self.stored.append((args[0], args[2]))
                return "OK", [b"1"]
            raise AssertionError(command)

    mail = LegacyPendingIntakeMail(make_raw_mail())
    bot._imap_connect = lambda _cfg: mail

    bot._poll_once_durable(bot.config.email, bot.config.bot)

    assert events == [("fetch", 12), ("store", "12")]
    assert bot._state.pending_intake("INBOX", 77) == []
    assert bot._state.pending_seen("INBOX") == []
    bot._state.close()


def test_uid_range_drops_server_returned_uid_below_high_water_mark(tmp_path):
    bot = make_bot(tmp_path)
    bot._state.set_mailbox_position("INBOX", 77, 100)

    class OldRangeMail(FakeImap):
        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"99" if args[1].startswith("UID ") else b""]
            raise AssertionError("an old UID must not be fetched")

    mail = OldRangeMail(make_raw_mail())
    bot._imap_connect = lambda _cfg: mail

    bot._poll_once_durable(bot.config.email, bot.config.bot)

    assert mail.stored == []
    bot._state.close()


def test_seen_ack_requires_strict_uid_store_success():
    class NonTupleStore:
        def __init__(self):
            self.uid_calls = []
            self.sequence_store_calls = []

        def uid(self, *args):
            self.uid_calls.append(args)
            return "OK"

        def store(self, *args):
            self.sequence_store_calls.append(args)
            return "OK", [b"1"]

    mail = NonTupleStore()

    assert _mark_seen_uid(mail, 9) is False
    assert mail.uid_calls == [("store", "9", "+FLAGS", "\\Seen")]
    assert mail.sequence_store_calls == []


@pytest.mark.parametrize("response", ["OK", ("OK",)])
def test_seen_ack_rejects_malformed_uid_store_result(response):
    class MalformedStore:
        def uid(self, *args):
            assert args == ("store", "9", "+FLAGS", "\\Seen")
            return response

    assert _mark_seen_uid(MalformedStore(), 9) is False


def test_route_failure_is_quarantined_and_acknowledged(tmp_path):
    bot = make_bot(tmp_path)
    mail = FakeImap(make_raw_cookie_mail())
    bot._imap_connect = lambda _cfg: mail
    bot._ensure_command_task = lambda *_args: (_ for _ in ()).throw(
        RuntimeError("simulated crash before notice")
    )

    bot._poll_once_durable(bot.config.email, bot.config.bot)

    source = bot._state._conn.execute(
        "SELECT metadata_json, intake_complete, seen_at FROM source_messages "
        "WHERE source_message_id = ?",
        ("INBOX:77:9",),
    ).fetchone()
    assert source["intake_complete"] == 1
    assert source["seen_at"] is not None
    assert "RuntimeError" in source["metadata_json"]
    assert "simulated crash before notice" not in source["metadata_json"]
    assert bot._state.pending_seen("INBOX") == []
    assert bot._state.pending_intake("INBOX", 77) == []
    assert mail.stored == [("9", "\\Seen")]
    bot._state.close()


def test_legacy_rollback_is_rejected_until_durable_work_is_drained(tmp_path):
    state_path = tmp_path / "state.sqlite"
    state = MailStateStore(state_path)
    task = state.enqueue_task("m1", "https://example.test/a")
    state.close()

    with pytest.raises(RuntimeError, match="drain intake/Seen/tasks/outbox"):
        EmailBot._assert_legacy_rollback_safe(state_path)

    state = MailStateStore(state_path)
    claimed = state.claim_tasks(now=100)[0]
    state.complete_task(task["id"], claimed["lease_token"], result={"ok": True}, now=101)
    state.close()

    EmailBot._assert_legacy_rollback_safe(state_path)


@pytest.mark.parametrize("contents", ["not json", "[]", '{"bad": {}}'])
def test_legacy_rollback_rejects_unreadable_or_malformed_retry_file(tmp_path, contents):
    state_path = tmp_path / "state.sqlite"
    MailStateStore(state_path).close()
    pending_path = tmp_path / "pending.json"
    pending_path.write_text(contents, encoding="utf-8")

    with pytest.raises(RuntimeError, match="legacy retry file"):
        EmailBot._assert_legacy_rollback_safe(state_path, pending_path)


def test_cookie_command_bypasses_subject_keyword_gate(tmp_path):
    bot = make_bot(tmp_path)
    mail = FakeImap(make_raw_cookie_mail())
    bot._imap_connect = lambda _cfg: mail

    bot._poll_once_durable(bot.config.email, bot.config.bot)

    task = bot._state._conn.execute(
        "SELECT normalized_url FROM tasks"
    ).fetchone()
    assert task["normalized_url"] == "urn:mail-notice:cookie_update"
    bot._state.close()


def test_cookie_command_secret_is_not_persisted_in_sqlite(tmp_path):
    bot = make_bot(tmp_path)
    secret = "sessionid=" + "s" * 128

    bot._ensure_command_task(
        "INBOX:77:12",
        {"sender": "user@example.test", "subject": "更新cookie"},
        secret,
        "cookie_update",
    )

    rows = bot._state._conn.execute(
        "SELECT payload_json FROM tasks UNION ALL SELECT payload_json FROM smtp_outbox"
    ).fetchall()
    assert rows
    assert all(secret not in row["payload_json"] for row in rows)
    assert str(len(secret)) in rows[-1]["payload_json"]
    bot._state.close()


def test_duplicate_cookie_command_does_not_repeat_side_effect(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    calls = []
    original = bot._process_cookie_command

    def counted(task):
        calls.append(task)
        return original(task)

    monkeypatch.setattr(bot, "_process_cookie_command", counted)
    metadata = {"sender": "user@example.test", "subject": "更新cookie"}
    secret = "sessionid=" + "s" * 128
    bot._ensure_command_task("INBOX:77:13", metadata, secret, "cookie_update")
    bot._ensure_command_task("INBOX:77:13", metadata, secret, "cookie_update")

    assert len(calls) == 1
    assert bot._state._conn.execute("SELECT COUNT(*) FROM smtp_outbox").fetchone()[0] == 1
    bot._state.close()


def test_ambiguous_smtp_failure_keeps_outbox_and_message_id(tmp_path):
    bot = make_bot(tmp_path)
    bot._state.clock = lambda: 100.0
    task = bot._state.enqueue_task("m1", "https://example.test/a")
    item = bot._state.enqueue_outbox(
        task["id"],
        "completed",
        {"to_addr": "user@example.test", "body": "done"},
    )
    claimed = bot._state.claim_outbox(now=100, lease_seconds=30)[0]
    message_ids = []

    def send_then_disconnect(*_args, **kwargs):
        message_ids.append(kwargs["message_id"])
        raise TimeoutError("connection lost after DATA")

    bot._send_reply = send_then_disconnect
    bot._deliver_outbox(claimed)

    row = bot._state._conn.execute(
        "SELECT status, message_id, next_attempt_at FROM smtp_outbox WHERE id = ?",
        (item["id"],),
    ).fetchone()
    assert row["status"] == "failed"
    assert row["message_id"] == item["message_id"]
    assert message_ids == [item["message_id"]]
    assert row["next_attempt_at"] is not None
    bot._state.close()
