from pathlib import Path
from types import SimpleNamespace

from config_loader import load_config
from f2_bootstrap import bootstrap_f2


bootstrap_f2()
from email_bot import (  # noqa: E402
    EmailBot,
    _format_success_reply,
    _format_failure_alert,
    _partial_failure_error,
    _redact_sensitive_text,
    _success_subject_status,
    _try_extract_cookie,
)


def test_optional_paths_resolve_from_config_directory_and_keep_empty(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
bilibili:
  auth_file: auth.json
cookie_extractor:
  profile_dir: firefox-profile
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("BILIBILI_AUTH_FILE", raising=False)
    monkeypatch.delenv("COOKIE_PROFILE_DIR", raising=False)

    config = load_config(config_path)

    assert config.bilibili.auth_file == str((tmp_path / "auth.json").resolve())
    assert config.cookie_extractor.profile_dir == str(
        (tmp_path / "firefox-profile").resolve()
    )

    config_path.write_text(
        "bilibili: {}\ncookie_extractor:\n  profile_dir: ''\n", encoding="utf-8"
    )
    config = load_config(config_path)
    assert config.bilibili.auth_file == ""
    assert config.cookie_extractor.profile_dir == ""


def test_codex_settings_support_yaml_and_environment_overrides(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
codex:
  flag_file: yaml-flags.json
  notify_email: yaml@example.com
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_FAILURE_FLAG_ENABLED", "false")
    monkeypatch.setenv("CODEX_FAILURE_FLAG_FILE", "state/env-flags.json")
    monkeypatch.setenv("CODEX_PROCESS_REQUEST_DIR", "state/requests")
    monkeypatch.setenv("CODEX_AUTO_INTERVAL_SECONDS", "42")
    monkeypatch.setenv("CODEX_NOTIFY_EMAIL", "env@example.com")

    config = load_config(config_path)

    assert config.codex.enabled is False
    assert config.codex.flag_file == str((tmp_path / "state/env-flags.json").resolve())
    assert config.codex.process_request_dir == str(
        (tmp_path / "state/requests").resolve()
    )
    assert config.codex.interval_seconds == 42
    assert config.codex.notify_email == "env@example.com"


def test_transient_retry_paths_honor_env_and_resolve_relative_to_config(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
bot:
  transient_pending_file: yaml-pending.json
  transient_failed_file: yaml-failed.txt
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BOT_TRANSIENT_PENDING_FILE", "state/pending.json")
    monkeypatch.setenv("BOT_TRANSIENT_FAILED_FILE", str(tmp_path / "env-failed.txt"))

    config = load_config(config_path)

    assert config.bot.transient_pending_file == str(
        (tmp_path / "state/pending.json").resolve()
    )
    assert config.bot.transient_failed_file == str(
        (tmp_path / "env-failed.txt").resolve()
    )


def test_transient_retry_paths_keep_yaml_defaults_without_env(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
bot:
  transient_pending_file: yaml-pending.json
  transient_failed_file: yaml-failed.txt
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("BOT_TRANSIENT_PENDING_FILE", raising=False)
    monkeypatch.delenv("BOT_TRANSIENT_FAILED_FILE", raising=False)

    config = load_config(config_path)

    assert config.bot.transient_pending_file == str(
        (tmp_path / "yaml-pending.json").resolve()
    )
    assert config.bot.transient_failed_file == str(
        (tmp_path / "yaml-failed.txt").resolve()
    )


def test_try_extract_cookie_forwards_configured_options(monkeypatch):
    captured = {}

    def fake_extract_cookies(**kwargs):
        captured.update(kwargs)
        return "cookie", "ok"

    import cookie_extractor

    monkeypatch.setattr(cookie_extractor, "extract_cookies", fake_extract_cookies)
    result = _try_extract_cookie(Path("profile"), headless=False, validate=False)

    assert result == ("cookie", "ok")
    assert captured == {
        "profile_dir": Path("profile"),
        "headless": False,
        "validate": False,
    }


def test_malformed_pending_retry_fields_do_not_abort_other_items(monkeypatch):
    bot = object.__new__(EmailBot)
    bot._pending_retries = {
        "bad-time": {
            "url": "https://example.invalid/bad-time",
            "next_attempt_at": "not-a-timestamp",
            "attempts": 1,
        },
        "bad-attempts": {
            "url": "https://example.invalid/bad-attempts",
            "next_attempt_at": 0,
            "attempts": "not-an-integer",
            "sender": "sender@example.com",
        },
    }
    bot._save_pending_retries = lambda: None
    bot._send_reply = lambda *args, **kwargs: None
    monkeypatch.setattr(
        bot,
        "_download_url",
        lambda url: {
            "success": True,
            "filepath": "/tmp/result.mp4",
            "title": "result",
            "files": [],
            "file_count": 0,
        },
    )

    bot._process_pending_retries(
        SimpleNamespace(),
        SimpleNamespace(transient_retry_attempts=3, transient_retry_delay_seconds=60),
    )

    assert "bad-time" not in bot._pending_retries
    assert "bad-attempts" not in bot._pending_retries


def test_partial_download_is_visible_in_reply_and_subject():
    result = {
        "title": "post",
        "partial": True,
        "files": ["/downloads/one.jpg"],
        "file_count": 1,
        "failed_count": 1,
        "failed_items": ["图片 two.jpg: offline"],
        "retry_queued": True,
    }

    reply = _format_success_reply(result, "/downloads/slides")
    assert "部分下载完成" in reply
    assert "1 个资源下载失败" in reply
    assert "two.jpg" in reply
    assert "已加入自动重试队列" in reply
    assert _success_subject_status(result) == "部分成功（1个文件）"
    assert _partial_failure_error(result) == "图片 two.jpg: offline"


def test_partial_transient_retry_remains_persisted(monkeypatch):
    bot = object.__new__(EmailBot)
    bot._pending_retries = {
        "retry": {
            "url": "https://example.invalid/post",
            "sender": "sender@example.com",
            "subject": "下载",
            "platform": "douyin",
            "next_attempt_at": 0,
            "attempts": 1,
        }
    }
    bot._save_pending_retries = lambda: None
    bot._send_reply = lambda *args, **kwargs: None
    bot._download_url = lambda url: {
        "success": True,
        "partial": True,
        "filepath": "/tmp/slides",
        "title": "post",
        "failed_items": ["图片 two.jpg: network timeout"],
    }
    monkeypatch.setattr("email_bot.time.time", lambda: 100.0)

    bot._process_pending_retries(
        SimpleNamespace(),
        SimpleNamespace(transient_retry_attempts=3, transient_retry_delay_seconds=60),
    )

    queued = bot._pending_retries["retry"]
    assert queued["attempts"] == 2
    assert queued["next_attempt_at"] == 160.0
    assert "network timeout" in queued["last_error"]


def test_failure_alert_contains_actionable_context(tmp_path):
    body = _format_failure_alert(
        url="https://v.douyin.com/example",
        platform="douyin",
        subject="下载：示例",
        error_msg="network timeout",
        attempts=2,
        retry_status="已加入自动重试队列",
        flag_status="已置位，等待宿主机 Codex 处理",
        pending_retry_file=tmp_path / "pending.json",
        failed_links_file=tmp_path / "failed.txt",
    )

    assert "https://v.douyin.com/example" in body
    assert "network timeout" in body
    assert "已加入自动重试队列" in body
    assert "失败 FLAG 状态：已置位，等待宿主机 Codex 处理" in body
    assert str(tmp_path / "failed.txt") in body


def test_failure_notifications_only_include_requester(monkeypatch):
    bot = object.__new__(EmailBot)
    bot.config = SimpleNamespace(
        codex=SimpleNamespace(notify_email="owner@example.com")
    )
    sent = []
    bot._send_reply = lambda cfg, recipient, body, subject_status: sent.append(
        recipient
    )
    cfg = SimpleNamespace(email="bot@example.com")

    bot._send_failure_notifications(
        cfg,
        "requester@example.com",
        "failure details",
        "下载失败",
    )
    assert sent == ["requester@example.com"]

    sent.clear()
    bot._send_failure_notifications(
        cfg, "owner@example.com", "failure details", "下载失败"
    )
    assert sent == ["owner@example.com"]


def test_partial_failure_notification_is_sent_only_to_requester(tmp_path):
    bot = object.__new__(EmailBot)
    bot.config = SimpleNamespace(
        codex=SimpleNamespace(notify_email="owner@example.com")
    )
    bot._pending_retry_file = tmp_path / "pending.json"
    bot._failed_links_file = tmp_path / "failed.txt"
    sent = []
    bot._send_reply = lambda cfg, recipient, body, subject_status: sent.append(
        (recipient, body, subject_status)
    )
    cfg = SimpleNamespace(email="bot@example.com", password="")

    bot._send_failure_notifications(
        cfg,
        "requester@example.com",
        "部分成功\n\n视频下载失败详细通知\n链接：https://example.invalid",
        "部分资源失败",
    )

    assert [recipient for recipient, _, _ in sent] == ["requester@example.com"]
    assert all("视频下载失败详细通知" in body for _, body, _ in sent)


def test_codex_process_command_writes_request_and_replies_only_to_sender(tmp_path):
    from failure_flag import ProcessRequestStore

    bot = object.__new__(EmailBot)
    bot.config = SimpleNamespace(
        codex=SimpleNamespace(process_request_dir=str(tmp_path / "requests"))
    )
    bot.extractor = SimpleNamespace(
        extract=lambda text: "https://example.invalid/video"
    )
    bot._process_requests = ProcessRequestStore(tmp_path / "requests")
    sent = []
    bot._send_reply = lambda cfg, recipient, body, subject_status: sent.append(
        (recipient, body, subject_status)
    )

    class Mail:
        def store(self, *args):
            self.seen = args

    bot._handle_codex_process(
        Mail(), b"7", SimpleNamespace(), "requester@example.com", "处理失败", "url"
    )

    requests = bot._process_requests.list_requests()
    assert len(requests) == 1
    assert requests[0][1]["sender"] == "requester@example.com"
    assert requests[0][1]["url"] == "https://example.invalid/video"
    assert [item[0] for item in sent] == ["requester@example.com"]
    assert "已请求宿主机 Codex 处理" in sent[0][1]
