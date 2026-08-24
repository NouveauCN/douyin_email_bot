from types import SimpleNamespace

import codex_failure_worker
from failure_flag import FailureFlagStore, ProcessRequestStore


def _config(tmp_path):
    return SimpleNamespace(
        email=SimpleNamespace(
            email="bot@example.com",
            password="password",
            smtp_server="smtp.example.com",
            smtp_port=587,
        ),
        codex=SimpleNamespace(
            enabled=True,
            flag_file=str(tmp_path / "flags.json"),
            process_request_dir=str(tmp_path / "requests"),
            executable="codex",
            sandbox="read-only",
            timeout_seconds=30,
            working_directory=str(tmp_path),
            model="",
            max_output_chars=1000,
            interval_seconds=3600,
        ),
    )


def _add_failure(config, sender="requester@example.com"):
    FailureFlagStore(config.codex.flag_file).set_failure(
        sender=sender,
        url="https://example.invalid/video",
        platform="douyin",
        subject="下载",
        error="network timeout",
        attempts=1,
        retry_status="queued",
    )


def test_worker_success_clears_flag_and_notifies_requester(monkeypatch, tmp_path):
    config = _config(tmp_path)
    _add_failure(config)
    sent = []

    monkeypatch.setattr(
        codex_failure_worker,
        "run_codex_failure_handler",
        lambda *args, **kwargs: SimpleNamespace(
            success=True, output="diagnosis", error=""
        ),
    )

    stats = codex_failure_worker.run_once(
        config, send_email=lambda *args: sent.append(args)
    )

    assert stats == {
        "processed": 1,
        "succeeded": 1,
        "failed": 0,
        "no_match": 0,
        "requests": 0,
    }
    assert FailureFlagStore(config.codex.flag_file).load() == {}
    assert [recipient for recipient, _, _ in sent] == ["requester@example.com"]


def test_worker_failure_retains_flag_for_next_hourly_scan(monkeypatch, tmp_path):
    config = _config(tmp_path)
    _add_failure(config)
    monkeypatch.setattr(
        codex_failure_worker,
        "run_codex_failure_handler",
        lambda *args, **kwargs: SimpleNamespace(
            success=False, output="", error="codex unavailable"
        ),
    )

    stats = codex_failure_worker.run_once(config, send_email=lambda *args: None)

    assert stats["processed"] == 1
    assert stats["failed"] == 1
    assert len(FailureFlagStore(config.codex.flag_file).load()) == 1


def test_worker_request_without_match_notifies_requester(monkeypatch, tmp_path):
    config = _config(tmp_path)
    ProcessRequestStore(config.codex.process_request_dir).create_request(
        sender="requester@example.com",
        subject="处理失败",
    )
    sent = []

    stats = codex_failure_worker.run_once(
        config, send_email=lambda *args: sent.append(args), process_flags=False
    )

    assert stats["requests"] == 1
    assert stats["no_match"] == 1
    assert [recipient for recipient, _, _ in sent] == ["requester@example.com"]
