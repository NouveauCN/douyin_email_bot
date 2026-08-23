from pathlib import Path
from types import SimpleNamespace

from config_loader import load_config
from f2_bootstrap import bootstrap_f2


bootstrap_f2()
from email_bot import (  # noqa: E402
    EmailBot,
    _format_success_reply,
    _partial_failure_error,
    _success_subject_status,
    _try_extract_cookie,
)


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
