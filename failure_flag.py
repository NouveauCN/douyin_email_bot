"""Persistent failure flags for host-side Codex diagnosis."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:EMAIL_PASSWORD|DOUYIN_COOKIE|BILIBILI_AUTH|OPENAI_API_KEY)\s*[=:]\s*[^\s]+"
)


def failure_flag_key(sender: str, url: str) -> str:
    """Return the stable, non-sensitive key for a sender/link pair."""
    value = f"{sender}\n{url}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _sanitize(value) -> str:
    return _SECRET_ASSIGNMENT_RE.sub("[敏感配置已隐藏]", str(value or ""))


class FailureFlagStore:
    """Read and atomically update a JSON object of failure contexts."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict:
        with _LOCK:
            try:
                if not self.path.exists():
                    return {}
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except (OSError, ValueError, TypeError) as exc:
                logger.warning("Failed to load failure flags %s: %s", self.path, exc)
                return {}

    def set_failure(
        self,
        *,
        sender: str,
        url: str,
        platform: str,
        subject: str,
        error: str,
        attempts: int | None,
        retry_status: str,
        key_sender: str | None = None,
        key_url: str | None = None,
        **context,
    ) -> bool:
        """Persist one redacted failure context, returning whether it was saved."""
        key = failure_flag_key(
            key_sender if key_sender is not None else sender,
            key_url if key_url is not None else url,
        )
        record = {
            "platform": _sanitize(platform),
            "url": _sanitize(url),
            "sender": _sanitize(sender),
            "subject": _sanitize(subject),
            "error": _sanitize(error),
            "attempts": attempts,
            "retry_status": _sanitize(retry_status),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        for name, value in context.items():
            if name not in record:
                record[name] = _sanitize(value)

        with _LOCK:
            flags = self.load()
            flags[key] = record
            return self._write(flags)

    def clear(self, sender: str, url: str) -> bool:
        """Remove a resolved failure context, returning whether it was saved."""
        key = failure_flag_key(sender, url)
        with _LOCK:
            flags = self.load()
            if key not in flags:
                return True
            del flags[key]
            return self._write(flags)

    def _write(self, flags: dict) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
                json.dump(flags, tmp, ensure_ascii=False, indent=2, sort_keys=True)
                tmp.write("\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, self.path)
            return True
        except OSError as exc:
            logger.error("Failed to save failure flags %s: %s", self.path, exc)
            return False
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
