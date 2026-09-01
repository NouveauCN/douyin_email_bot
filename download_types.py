"""Public contracts for the in-process download task service.

The mail bot used to pass loosely shaped dictionaries between intake, worker,
and notification code.  These small value objects are deliberately free of
SQLite, Flask, SMTP, and downloader imports so another entry point can use the
same task service without inheriting mail-specific behavior.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, TypeAlias


JSONPrimitive: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONPrimitive | list["JSONValue"] | dict[str, "JSONValue"]
Metadata: TypeAlias = dict[str, JSONValue]


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIALLY_SUCCEEDED = "partially_succeeded"
    FAILED = "failed"


class RetryClass(str, Enum):
    NONE = "none"
    TRANSIENT = "transient"
    PERMANENT = "permanent"


class ErrorCode(str, Enum):
    UNSUPPORTED_URL = "unsupported_url"
    INVALID_URL = "invalid_url"
    DOWNLOAD_FAILED = "download_failed"
    NETWORK = "network"
    TIMEOUT = "timeout"
    COOKIE_REQUIRED = "cookie_required"
    UNKNOWN = "unknown"


def _validate_json(value: Any, *, depth: int = 0) -> None:
    """Reject non-JSON metadata and pathological nesting before persistence."""
    if depth > 8:
        raise ValueError("metadata nesting is too deep")
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    if isinstance(value, list | tuple):
        if len(value) > 256:
            raise ValueError("metadata list is too large")
        for item in value:
            _validate_json(item, depth=depth + 1)
        return
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise ValueError("metadata object is too large")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 256:
                raise ValueError("metadata keys must be short strings")
            _validate_json(item, depth=depth + 1)
        return
    raise TypeError(f"metadata contains non-JSON value: {type(value).__name__}")


def _validate_identifier(value: str, field_name: str) -> str:
    value = str(value).strip()
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    if len(value) > 512 or "\x00" in value:
        raise ValueError(f"{field_name} is invalid")
    return value


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if any(part in str(key).lower() for part in ("cookie", "password", "passwd", "secret", "auth", "token")):
                return True
            if _contains_secret_key(nested):
                return True
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_key(item) for item in value)
    return False


def validate_metadata(value: Mapping[str, JSONValue] | None) -> Metadata:
    """Validate and copy non-secret JSON metadata at a public boundary."""
    metadata: Metadata = dict(value or {})
    _validate_json(metadata)
    if _contains_secret_key(metadata):
        raise ValueError("metadata must not contain secret-like fields")
    encoded = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise ValueError("metadata is too large")
    return metadata


@dataclass(frozen=True, slots=True)
class SourceRef:
    """An idempotency namespace and identifier owned by an entry point."""

    kind: str
    external_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _validate_identifier(self.kind, "source kind"))
        object.__setattr__(
            self,
            "external_id",
            _validate_identifier(self.external_id, "source external_id"),
        )

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.external_id}"


@dataclass(frozen=True, slots=True, init=False)
class TaskRequest:
    """A single URL request submitted by an entry adapter.

    ``source_id=`` remains accepted as a compatibility spelling for early
    callers of the plan.  New code should pass ``source=SourceRef(...)``.
    """

    url: str
    source: SourceRef
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    def __init__(
        self,
        url: str,
        source: SourceRef | str | None = None,
        metadata: Mapping[str, JSONValue] | None = None,
        *,
        source_id: str | None = None,
        source_kind: str = "generic",
    ) -> None:
        if source is not None and source_id is not None:
            raise TypeError("pass source or source_id, not both")
        if source is None:
            if source_id is None:
                raise TypeError("source or source_id is required")
            source = SourceRef(source_kind, source_id)
        elif isinstance(source, str):
            source = SourceRef(source_kind, source)
        if not isinstance(source, SourceRef):
            raise TypeError("source must be a SourceRef or string")
        value = str(url).strip()
        if not value:
            raise ValueError("url must not be empty")
        if len(value) > 8192 or "\x00" in value:
            raise ValueError("url is invalid")
        clean_metadata = validate_metadata(metadata)
        object.__setattr__(self, "url", value)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "metadata", clean_metadata)

    @property
    def source_id(self) -> str:
        return self.source.external_id

    @property
    def source_kind(self) -> str:
        return self.source.kind


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Normalized result shared by all platform adapters."""

    success: bool
    filepath: str | None = None
    files: tuple[str, ...] = ()
    file_count: int = 0
    covers: tuple[str, ...] = ()
    title: str | None = None
    error: str | None = None
    partial: bool = False
    failed_count: int = 0
    failed_items: tuple[str, ...] = ()
    retryable: bool = False
    error_code: ErrorCode = ErrorCode.UNKNOWN
    retry_class: RetryClass = RetryClass.NONE

    def __post_init__(self) -> None:
        files = tuple(str(item) for item in self.files)
        covers = tuple(str(item) for item in self.covers)
        failed_items = tuple(str(item) for item in self.failed_items)
        count = max(0, int(self.file_count or len(files)))
        failed_count = max(0, int(self.failed_count or len(failed_items)))
        retry_class = self.retry_class
        if isinstance(retry_class, str):
            retry_class = RetryClass(retry_class)
        retryable = bool(self.retryable or retry_class == RetryClass.TRANSIENT)
        partial = bool(self.partial or (self.success and failed_count > 0))
        if partial and retry_class == RetryClass.NONE:
            failed_text = " ".join(failed_items).lower()
            if any(part in failed_text for part in ("timeout", "timed out", "network", "connection", "超时", "网络", "连接")):
                retry_class = RetryClass.TRANSIENT
                retryable = True
        object.__setattr__(self, "files", files)
        object.__setattr__(self, "covers", covers)
        object.__setattr__(self, "failed_items", failed_items)
        object.__setattr__(self, "file_count", count)
        object.__setattr__(self, "failed_count", failed_count)
        object.__setattr__(self, "retry_class", retry_class)
        object.__setattr__(self, "retryable", retryable)
        object.__setattr__(self, "partial", partial)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | "DownloadResult") -> "DownloadResult":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("download result must be a mapping")
        raw_code = value.get("error_code", ErrorCode.UNKNOWN)
        try:
            code = raw_code if isinstance(raw_code, ErrorCode) else ErrorCode(str(raw_code))
        except ValueError:
            code = ErrorCode.UNKNOWN
        raw_retry = value.get("retry_class", RetryClass.NONE)
        try:
            retry_class = raw_retry if isinstance(raw_retry, RetryClass) else RetryClass(str(raw_retry))
        except ValueError:
            retry_class = RetryClass.TRANSIENT if value.get("retryable") else RetryClass.NONE
        if retry_class == RetryClass.NONE and value.get("retryable"):
            retry_class = RetryClass.TRANSIENT
        return cls(
            success=bool(value.get("success")),
            filepath=str(value["filepath"]) if value.get("filepath") is not None else None,
            files=tuple(value.get("files") or ()),
            file_count=int(value.get("file_count") or 0),
            covers=tuple(value.get("covers") or ()),
            title=str(value["title"]) if value.get("title") is not None else None,
            error=str(value["error"]) if value.get("error") is not None else None,
            partial=bool(value.get("partial")),
            failed_count=int(value.get("failed_count") or 0),
            failed_items=tuple(value.get("failed_items") or ()),
            retryable=bool(value.get("retryable")),
            error_code=code,
            retry_class=retry_class,
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "success": self.success,
            "filepath": self.filepath,
            "files": list(self.files),
            "file_count": self.file_count,
            "covers": list(self.covers),
            "title": self.title,
            "error": self.error,
            "partial": self.partial,
            "failed_count": self.failed_count,
            "failed_items": list(self.failed_items),
            "retryable": self.retryable,
            "error_code": self.error_code.value,
            "retry_class": self.retry_class.value,
        }


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    task_id: int
    url: str
    platform: str | None
    status: TaskStatus
    attempts: int
    result: DownloadResult | None = None
    last_error: str | None = None
    source: SourceRef | None = None
    created_at: float | None = None
    updated_at: float | None = None
    completed_at: float | None = None
    next_attempt_at: float | None = None
    lease_token: str | None = None
    lease_expires_at: float | None = None
    max_attempts: int | None = None
    metadata: Metadata = field(default_factory=dict)

    @classmethod
    def from_record(cls, row: Mapping[str, Any]) -> "TaskSnapshot":
        status_value = str(row.get("status", TaskStatus.PENDING.value))
        if status_value == "leased":
            status_value = TaskStatus.RUNNING.value
        result_value = row.get("result")
        result = DownloadResult.from_mapping(result_value) if isinstance(result_value, Mapping) else None
        # v2 databases cannot accept the new physical status until rebuilt;
        # the normalized result still gives callers the logical state.
        if status_value == TaskStatus.SUCCEEDED.value and result is not None and result.partial:
            status_value = TaskStatus.PARTIALLY_SUCCEEDED.value
        try:
            status = TaskStatus(status_value)
        except ValueError:
            status = TaskStatus.FAILED
        source = None
        kind = row.get("source_kind")
        external_id = row.get("external_source_id")
        if kind and external_id:
            source = SourceRef(str(kind), str(external_id))
        return cls(
            task_id=int(row["id"] if "id" in row else row["task_id"]),
            url=str(row.get("original_url") or row.get("url") or ""),
            platform=str(row["platform"]) if row.get("platform") else None,
            status=status,
            attempts=int(row.get("attempts") or 0),
            result=result,
            last_error=row.get("last_error"),
            source=source,
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            completed_at=row.get("completed_at"),
            next_attempt_at=row.get("next_attempt_at"),
            lease_token=row.get("lease_token"),
            lease_expires_at=row.get("lease_expires_at"),
            max_attempts=int(row["max_attempts"]) if row.get("max_attempts") is not None else None,
            metadata=validate_metadata(row.get("payload") if isinstance(row.get("payload"), Mapping) else {}),
        )


__all__ = [
    "DownloadResult",
    "ErrorCode",
    "JSONValue",
    "Metadata",
    "RetryClass",
    "SourceRef",
    "TaskRequest",
    "TaskSnapshot",
    "TaskStatus",
    "validate_metadata",
]
