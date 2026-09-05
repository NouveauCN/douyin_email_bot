"""Pure download outcome and retry decisions.

This module intentionally knows nothing about workers or persistence.  Keeping
the decision in one place prevents durable and legacy execution paths from
quietly acquiring different retry semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from download_types import DownloadResult, ErrorCode, RetryClass

Action = Literal["retry", "complete", "fail"]
RISK_CONTROL_RETRY_FLOOR_SECONDS = 30 * 60


@dataclass(frozen=True, slots=True)
class OutcomeDecision:
    action: Action
    result: DownloadResult
    error: str | None = None
    retry_at: float | None = None
    status: str | None = None

    @property
    def terminal(self) -> bool:
        return self.action in ("complete", "fail")


def classify_result(result: DownloadResult) -> DownloadResult:
    """Fill stable retry metadata for older platform adapters."""
    if result.success:
        return result
    if result.error_code != ErrorCode.UNKNOWN or result.retry_class != RetryClass.NONE:
        return result
    text = (result.error or "").lower()
    if any(part in text for part in ("timeout", "timed out", "超时")):
        code, retry = ErrorCode.TIMEOUT, RetryClass.TRANSIENT
    elif any(part in text for part in ("network", "connection", "网络", "连接")):
        code, retry = ErrorCode.NETWORK, RetryClass.TRANSIENT
    elif any(part in text for part in ("cookie", "登录", "auth", "authentication", "access denied", "http 401", "http 403")):
        code, retry = ErrorCode.COOKIE_REQUIRED, RetryClass.TRANSIENT
    elif any(part in text for part in ("私密", "删除", "不存在", "not found")):
        # Missing/private content is a terminal content outcome, not a cue to
        # repeatedly refresh an otherwise valid login session.
        code, retry = ErrorCode.DOWNLOAD_FAILED, RetryClass.PERMANENT
    else:
        code, retry = ErrorCode.DOWNLOAD_FAILED, RetryClass.PERMANENT
    return DownloadResult(
        **{
            **result.to_dict(),
            "error_code": code,
            "retry_class": retry,
            "retryable": retry == RetryClass.TRANSIENT,
        }
    )


def decide_outcome(
    result: DownloadResult,
    *,
    attempts: int,
    max_attempts: int,
    now: float,
    retry_delay_seconds: float,
) -> OutcomeDecision:
    """Return the persistence action for one completed adapter call."""
    if attempts < 0 or max_attempts < 1:
        raise ValueError("attempts must be non-negative and max_attempts positive")
    if result.success and not (result.partial and result.retryable):
        return OutcomeDecision(
            action="complete",
            result=result,
            status="partially_succeeded" if result.partial else "succeeded",
        )
    if result.retryable and attempts < max_attempts:
        detail = result.error or "；".join(result.failed_items)
        delay = max(0.0, float(retry_delay_seconds))
        # A metadata 401/403 is an account/session risk-control response.  A
        # normal transient delay can create a rapid retry loop and worsen it.
        if result.error_code == ErrorCode.COOKIE_REQUIRED or any(part in (detail or "").lower() for part in (
            "风险控制", "风控", "risk control", "risk-control", "access denied (http 403)",
        )):
            delay = max(delay, RISK_CONTROL_RETRY_FLOOR_SECONDS)
        return OutcomeDecision(
            action="retry",
            result=result,
            error=detail or ("partial download" if result.partial else "download failed"),
            retry_at=float(now) + delay,
        )
    if result.success:
        return OutcomeDecision(action="complete", result=result, status="partially_succeeded")
    return OutcomeDecision(
        action="fail",
        result=result,
        error=result.error or "download failed",
    )


def decide_exception(
    error: BaseException,
    *,
    attempts: int,
    max_attempts: int,
    now: float,
    retry_delay_seconds: float,
) -> OutcomeDecision:
    """Use the existing transient-then-terminal policy for worker errors."""
    transient = attempts < max_attempts
    result = DownloadResult(
        success=False,
        error=str(error),
        error_code=ErrorCode.UNKNOWN,
        retry_class=RetryClass.TRANSIENT if transient else RetryClass.PERMANENT,
        retryable=transient,
    )
    return OutcomeDecision(
        action="retry" if transient else "fail",
        result=result,
        error=str(error),
        retry_at=float(now) + max(0.0, float(retry_delay_seconds)) if transient else None,
    )


__all__ = [
    "OutcomeDecision",
    "classify_result",
    "decide_exception",
    "decide_outcome",
]
