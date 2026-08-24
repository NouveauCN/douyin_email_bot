"""Host-side worker for processing redacted download failure FLAGs.

This module deliberately imports neither EmailBot nor any downloader.  Run it
on the host where the Codex executable and its authentication are available.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import smtplib
import time
from email.mime.text import MIMEText
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

from codex_failure_handler import run_codex_failure_handler
from config_loader import load_config
from failure_flag import FailureFlagStore, ProcessRequestStore, failure_flag_key

logger = logging.getLogger("codex_failure_worker")
SendEmail = Callable[[str, str, str], None]
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:EMAIL_PASSWORD|DOUYIN_COOKIE|BILIBILI_AUTH|OPENAI_API_KEY)\s*[=:]\s*[^\s]+"
)


def _redact_sensitive_text(text: object, config) -> str:
    redacted = _SECRET_ASSIGNMENT_RE.sub("[敏感配置已隐藏]", str(text or ""))
    values = [
        getattr(getattr(config, "email", None), "password", ""),
        getattr(getattr(config, "douyin", None), "cookie", ""),
        getattr(getattr(config, "bilibili", None), "auth", ""),
        os.getenv("OPENAI_API_KEY", ""),
    ]
    for value in values:
        if value and len(value) >= 4:
            redacted = redacted.replace(value, "[敏感信息已隐藏]")
    return redacted


def _send_email(config, recipient: str, subject: str, body: str) -> None:
    """Send one result to exactly the requested failure record's sender."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = config.email
    msg["To"] = recipient
    msg["Subject"] = subject
    with smtplib.SMTP(config.smtp_server, config.smtp_port) as smtp:
        smtp.starttls()
        smtp.login(config.email, config.password)
        smtp.send_message(msg)


def filter_failure_flags(
    flags: dict,
    *,
    sender: str | None = None,
    url: str | None = None,
) -> list[tuple[str, dict]]:
    """Return valid failure records matching optional exact request filters."""
    matches = []
    for key, record in flags.items():
        if (
            not isinstance(record, dict)
            or not record.get("sender")
            or not record.get("url")
        ):
            continue
        if sender is not None and record.get("sender") != sender:
            continue
        if url is not None and record.get("url") != url:
            continue
        matches.append((str(key), record))
    return matches


def _diagnosis_message(record: dict, result, config) -> tuple[str, str]:
    subject = "Codex失败诊断结果" if result.success else "Codex失败诊断暂未完成"
    lines = [
        "宿主机 Codex 失败诊断",
        f"链接：{_redact_sensitive_text(record.get('url', ''), config)}",
        f"错误：{_redact_sensitive_text(record.get('error', ''), config)}",
        f"执行状态：{'成功' if result.success else '失败'}",
    ]
    if result.output:
        lines.extend(
            ["", "Codex 输出：", _redact_sensitive_text(result.output, config)]
        )
    if result.error:
        lines.extend(
            [
                "",
                f"执行错误：{_redact_sensitive_text(result.error, config)}",
            ]
        )
    return subject, "\n".join(lines)


def _notify(send_email: SendEmail, recipient: str, subject: str, body: str) -> bool:
    try:
        send_email(recipient, subject, body)
        return True
    except Exception:
        logger.exception("Failed to send Codex result to %s", recipient)
        return False


def _process_record(config, record: dict, send_email: SendEmail):
    codex = config.codex
    result = run_codex_failure_handler(
        codex.executable,
        codex.sandbox,
        codex.timeout_seconds,
        Path(codex.working_directory),
        record,
        model=codex.model,
        max_output_chars=codex.max_output_chars,
    )
    subject, body = _diagnosis_message(record, result, config)
    notified = _notify(send_email, str(record["sender"]), subject, body)
    return result, notified


def run_once(
    config,
    send_email: SendEmail | None = None,
    *,
    process_flags: bool = True,
) -> dict[str, int]:
    """Process pending requests and, optionally, all current failure FLAGs.

    ``send_email`` is injectable for tests and host integrations.  A failed
    Codex invocation leaves its failure FLAG in place for a later scan.
    """
    codex = config.codex
    stats = {"processed": 0, "succeeded": 0, "failed": 0, "no_match": 0, "requests": 0}
    if not getattr(codex, "enabled", True):
        logger.info("Codex failure worker is disabled")
        return stats

    if send_email is None:
        send_email = lambda recipient, subject, body: _send_email(
            config.email, recipient, subject, body
        )

    flags_store = FailureFlagStore(codex.flag_file)
    request_store = ProcessRequestStore(codex.process_request_dir)
    flags = flags_store.load()
    attempted_keys: set[str] = set()

    for request_path, request in request_store.list_requests():
        stats["requests"] += 1
        sender = str(request.get("sender", ""))
        requested_url = str(request.get("url", "")) or None
        matches = filter_failure_flags(flags, sender=sender, url=requested_url)
        if not matches:
            stats["no_match"] += 1
            _notify(
                send_email,
                sender,
                "Codex失败处理请求结果",
                "未找到与本次请求匹配的失败记录。",
            )
        for key, record in matches:
            attempted_keys.add(key)
            stats["processed"] += 1
            result, notified = _process_record(config, record, send_email)
            if result.success and notified:
                if flags_store.clear(record["sender"], record["url"]):
                    flags.pop(key, None)
                stats["succeeded"] += 1
            else:
                stats["failed"] += 1
        # A request is a one-shot trigger; failed FLAGs remain for retry.
        request_store.delete_request(request_path)

    if process_flags:
        for key, record in filter_failure_flags(flags):
            if key in attempted_keys:
                continue
            stats["processed"] += 1
            result, notified = _process_record(config, record, send_email)
            if result.success and notified:
                if flags_store.clear(record["sender"], record["url"]):
                    flags.pop(key, None)
                stats["succeeded"] += 1
            else:
                stats["failed"] += 1
    return stats


def watch(config, send_email: SendEmail | None = None) -> None:
    """Run hourly FLAG scans while checking request flags approximately every second."""
    interval = max(1, int(config.codex.interval_seconds))
    request_store = ProcessRequestStore(config.codex.process_request_dir)
    run_once(config, send_email)
    next_scan = time.monotonic() + interval
    while True:
        if request_store.list_requests():
            run_once(config, send_email, process_flags=False)
            next_scan = min(next_scan, time.monotonic() + interval)
            continue
        remaining = next_scan - time.monotonic()
        if remaining <= 0:
            run_once(config, send_email)
            next_scan = time.monotonic() + interval
            continue
        time.sleep(min(1.0, remaining))


def main() -> None:
    parser = argparse.ArgumentParser(description="Host-side Codex failure FLAG worker")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="process current flags once")
    mode.add_argument(
        "--watch", action="store_true", help="watch requests and scan periodically"
    )
    args = parser.parse_args()

    config_path = args.config.resolve()
    load_dotenv(config_path.parent / ".env")
    config = load_config(config_path)
    logging.basicConfig(level=logging.INFO)
    if args.watch:
        watch(config)
    else:
        run_once(config)


if __name__ == "__main__":
    main()
