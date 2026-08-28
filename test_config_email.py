from types import SimpleNamespace

from config_loader import load_config
from f2_bootstrap import bootstrap_f2


bootstrap_f2()
from email_bot import (  # noqa: E402
    EmailBot,
    _format_success_reply,
    _partial_failure_error,
    _success_subject_status,
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


def test_transient_retry_paths_honor_env_and_resolve_relative_to_config(tmp_path, monkeypatch):
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

    assert config.bot.transient_pending_file == str((tmp_path / "state/pending.json").resolve())
    assert config.bot.transient_failed_file == str((tmp_path / "env-failed.txt").resolve())


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

    assert config.bot.transient_pending_file == str((tmp_path / "yaml-pending.json").resolve())
    assert config.bot.transient_failed_file == str((tmp_path / "yaml-failed.txt").resolve())


def test_durable_mail_settings_resolve_and_honor_environment(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
bot:
  durable_mail_enabled: false
  state_db: yaml-state.sqlite
  worker_count: 4
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BOT_DURABLE_MAIL_ENABLED", "1")
    monkeypatch.setenv("BOT_STATE_DB", "state/env.sqlite")
    monkeypatch.setenv("BOT_WORKER_COUNT", "3")
    config = load_config(config_path)

    assert config.bot.durable_mail_enabled is True
    assert config.bot.state_db == str((tmp_path / "state/env.sqlite").resolve())
    assert config.bot.worker_count == 3




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
