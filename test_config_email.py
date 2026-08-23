from pathlib import Path
from types import SimpleNamespace

from config_loader import load_config
from f2_bootstrap import bootstrap_f2


bootstrap_f2()
from email_bot import (  # noqa: E402
    EmailBot,
    _format_success_reply,
    _format_codex_result,
    _format_failure_alert,
    _partial_failure_error,
    _redact_sensitive_text,
    _success_subject_status,
    _try_extract_cookie,
)
from codex_failure_handler import CodexRunResult


def test_optional_paths_resolve_from_config_directory_and_keep_empty(tmp_path, monkeypatch):
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
    assert config.cookie_extractor.profile_dir == str((tmp_path / "firefox-profile").resolve())

    config_path.write_text("bilibili: {}\ncookie_extractor:\n  profile_dir: ''\n", encoding="utf-8")
    config = load_config(config_path)
    assert config.bilibili.auth_file == ""
    assert config.cookie_extractor.profile_dir == ""


def test_codex_settings_support_yaml_and_environment_overrides(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
codex:
  enabled: false
  executable: yaml-codex
  sandbox: read-only
  timeout_seconds: 45
  working_directory: codex-work
  model: yaml-model
  max_output_chars: 2000
  notify_email: yaml@example.com
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_FAILURE_HANDLER_ENABLED", "true")
    monkeypatch.setenv("CODEX_BIN", "env-codex")
    monkeypatch.setenv("CODEX_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("CODEX_NOTIFY_EMAIL", "env@example.com")

    config = load_config(config_path)

    assert config.codex.enabled is True
    assert config.codex.executable == "env-codex"
    assert config.codex.timeout_seconds == 60
    assert config.codex.working_directory == str((tmp_path / "codex-work").resolve())
    assert config.codex.model == "yaml-model"
    assert config.codex.max_output_chars == 2000
    assert config.codex.notify_email == "env@example.com"


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
        codex_status="已唤起，后台诊断中",
        pending_retry_file=tmp_path / "pending.json",
        failed_links_file=tmp_path / "failed.txt",
    )

    assert "https://v.douyin.com/example" in body
    assert "network timeout" in body
    assert "已加入自动重试队列" in body
    assert "Codex处理状态：已唤起" in body
    assert str(tmp_path / "failed.txt") in body


def test_codex_context_and_output_redact_secrets():
    config = SimpleNamespace(
        douyin=SimpleNamespace(cookie="cookie-secret"),
        bilibili=SimpleNamespace(auth="auth-secret"),
    )
    cfg = SimpleNamespace(password="mail-secret")

    assert "cookie-secret" not in _redact_sensitive_text("cookie-secret", config, cfg)
    assert "auth-secret" not in _redact_sensitive_text("auth-secret", config, cfg)
    assert "mail-secret" not in _redact_sensitive_text("mail-secret", config, cfg)
    assert "DOUYIN_COOKIE=hidden" not in _redact_sensitive_text(
        "DOUYIN_COOKIE=hidden", config, cfg
    )

    result = _format_codex_result(
        {"url": "https://example.invalid", "platform": "douyin", "error": "failed"},
        CodexRunResult(True, True, 0, "cookie-secret\n诊断完成", "", 0.2),
        config,
        cfg,
    )
    assert "cookie-secret" not in result
    assert "诊断完成" in result


def test_codex_failure_is_started_once_per_link(monkeypatch, tmp_path):
    bot = object.__new__(EmailBot)
    bot.config = SimpleNamespace(
        codex=SimpleNamespace(
            enabled=True,
            notify_email="owner@example.com",
            working_directory="",
        ),
        douyin=SimpleNamespace(cookie=""),
        bilibili=SimpleNamespace(auth=""),
    )
    bot._project_dir = tmp_path
    bot._pending_retry_file = tmp_path / "pending.json"
    bot._failed_links_file = tmp_path / "failed.txt"
    bot._codex_inflight = set()
    import threading

    bot._codex_lock = threading.Lock()
    starts = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            starts.append(self.kwargs)

    monkeypatch.setattr("email_bot.threading.Thread", FakeThread)
    cfg = SimpleNamespace(email="bot@example.com", password="secret")

    first = bot._trigger_codex_failure(
        cfg=cfg,
        sender="owner@example.com",
        subject="下载",
        url="https://example.invalid/video",
        platform="douyin",
        error_msg="timeout",
        attempts=1,
        retry_status="queued",
    )
    second = bot._trigger_codex_failure(
        cfg=cfg,
        sender="owner@example.com",
        subject="下载",
        url="https://example.invalid/video",
        platform="douyin",
        error_msg="timeout again",
        attempts=2,
        retry_status="queued",
    )

    assert first == "已唤起，后台诊断中"
    assert "已有 Codex" in second
    assert len(starts) == 1


def test_failure_notifications_include_owner_without_duplicate(monkeypatch):
    bot = object.__new__(EmailBot)
    bot.config = SimpleNamespace(codex=SimpleNamespace(notify_email="owner@example.com"))
    sent = []
    bot._send_reply = lambda cfg, recipient, body, subject_status: sent.append(recipient)
    cfg = SimpleNamespace(email="bot@example.com")

    bot._send_failure_notifications(
        cfg,
        "requester@example.com",
        "failure details",
        "下载失败",
    )
    assert sent == ["requester@example.com", "owner@example.com"]

    sent.clear()
    bot._send_failure_notifications(cfg, "owner@example.com", "failure details", "下载失败")
    assert sent == ["owner@example.com"]


def test_partial_failure_notification_is_sent_to_requester_and_owner(tmp_path):
    bot = object.__new__(EmailBot)
    bot.config = SimpleNamespace(codex=SimpleNamespace(notify_email="owner@example.com"))
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

    assert [recipient for recipient, _, _ in sent] == [
        "requester@example.com",
        "owner@example.com",
    ]
    assert all("视频下载失败详细通知" in body for _, body, _ in sent)
