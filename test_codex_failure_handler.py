from pathlib import Path
import subprocess

import codex_failure_handler
from codex_failure_handler import CodexRunResult, run_codex_failure_handler


def test_command_parameters_and_model(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "diagnosis", "")

    monkeypatch.setattr(codex_failure_handler.subprocess, "run", fake_run)

    result = run_codex_failure_handler(
        "codex-bin",
        "workspace-write",
        17,
        tmp_path,
        {"url": "https://example.invalid/video", "error": "timeout"},
        model="gpt-test",
    )

    assert result == CodexRunResult(True, True, 0, "diagnosis", "", result.duration_seconds)
    command, kwargs = calls[0]
    assert command[:7] == [
        "codex-bin",
        "exec",
        "--ephemeral",
        "--sandbox",
        "workspace-write",
        "--model",
        "gpt-test",
    ]
    assert "https://example.invalid/video" in command[-1]
    assert "timeout" in command[-1]
    assert "untrusted" in command[-1].lower()
    assert kwargs == {
        "capture_output": True,
        "check": False,
        "cwd": tmp_path,
        "env": codex_failure_handler._codex_environment(),
        "shell": False,
        "text": True,
        "timeout": 17,
    }


def test_application_secrets_are_removed_from_codex_environment(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "mail-secret")
    monkeypatch.setenv("DOUYIN_COOKIE", "cookie-secret")
    monkeypatch.setenv("BILIBILI_AUTH", "auth-secret")
    monkeypatch.setenv("BILIBILI_AUTH_FILE", "/private/auth.json")
    monkeypatch.setenv("COOKIE_PROFILE_DIR", "/private/profile")
    monkeypatch.setenv("OPENAI_API_KEY", "codex-auth-is-preserved")

    environment = codex_failure_handler._codex_environment()

    assert "EMAIL_PASSWORD" not in environment
    assert "DOUYIN_COOKIE" not in environment
    assert "BILIBILI_AUTH" not in environment
    assert "BILIBILI_AUTH_FILE" not in environment
    assert "COOKIE_PROFILE_DIR" not in environment
    assert environment["OPENAI_API_KEY"] == "codex-auth-is-preserved"


def test_empty_model_is_omitted(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(codex_failure_handler.subprocess, "run", fake_run)
    run_codex_failure_handler("codex", "read-only", 1, tmp_path, {})

    assert "--model" not in captured["command"]


def test_invalid_sandbox_does_not_start_process(monkeypatch, tmp_path):
    def fail_run(*args, **kwargs):
        raise AssertionError("subprocess must not be called")

    monkeypatch.setattr(codex_failure_handler.subprocess, "run", fail_run)

    result = run_codex_failure_handler("codex", "invalid", 1, tmp_path, {})

    assert not result.started
    assert not result.success
    assert result.returncode is None
    assert "invalid sandbox" in result.error


def test_missing_executable(monkeypatch, tmp_path):
    monkeypatch.setattr(
        codex_failure_handler.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )

    result = run_codex_failure_handler("missing-codex", "read-only", 1, tmp_path, {})

    assert not result.started
    assert not result.success
    assert result.returncode is None
    assert "not found" in result.error


def test_timeout_returns_captured_output(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=args[0], timeout=kwargs["timeout"], output="partial", stderr="warning"
        )

    monkeypatch.setattr(codex_failure_handler.subprocess, "run", fake_run)

    result = run_codex_failure_handler("codex", "read-only", 3, tmp_path, {})

    assert result.started
    assert not result.success
    assert result.returncode is None
    assert result.output == "partial\nwarning"
    assert "timed out" in result.error


def test_stdout_and_stderr_are_truncated(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, "abcdefgh", "ijklmnop")

    monkeypatch.setattr(codex_failure_handler.subprocess, "run", fake_run)

    result = run_codex_failure_handler(
        "codex", "danger-full-access", 1, Path(tmp_path), {}, max_output_chars=9
    )

    assert result.output == "abcdefgh\n"
    assert len(result.output) <= 9
    assert not result.success
    assert result.returncode == 1
    assert "return code 1" in result.error
