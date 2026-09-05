from download_types import DownloadResult, ErrorCode, RetryClass
from outcome_policy import classify_result, decide_exception, decide_outcome


def test_classify_result_keeps_legacy_transient_error_semantics():
    result = classify_result(DownloadResult(success=False, error="network timeout"))
    assert result.error_code == ErrorCode.TIMEOUT
    assert result.retry_class == RetryClass.TRANSIENT
    assert result.retryable is True


def test_classify_result_treats_access_denied_as_cookie_refresh():
    result = classify_result(DownloadResult(
        success=False,
        error="Douyin media access denied (HTTP 403)",
    ))
    assert result.error_code == ErrorCode.COOKIE_REQUIRED
    assert result.retry_class == RetryClass.TRANSIENT
    assert result.retryable is True


def test_classify_result_keeps_missing_content_terminal():
    result = classify_result(DownloadResult(success=False, error="视频已被作者删除"))
    assert result.error_code == ErrorCode.DOWNLOAD_FAILED
    assert result.retry_class == RetryClass.PERMANENT
    assert result.retryable is False


def test_partial_transient_result_retries_until_attempt_limit():
    result = DownloadResult(
        success=True,
        partial=True,
        retryable=True,
        retry_class=RetryClass.TRANSIENT,
        failed_items=("item: timeout",),
    )
    decision = decide_outcome(
        result,
        attempts=1,
        max_attempts=2,
        now=100,
        retry_delay_seconds=10,
    )
    assert decision.action == "retry"
    assert decision.retry_at == 110
    assert decision.error == "item: timeout"

    terminal = decide_outcome(
        result,
        attempts=2,
        max_attempts=2,
        now=100,
        retry_delay_seconds=10,
    )
    assert terminal.action == "complete"
    assert terminal.status == "partially_succeeded"


def test_exception_policy_switches_to_terminal_at_limit():
    decision = decide_exception(
        RuntimeError("boom"),
        attempts=2,
        max_attempts=2,
        now=100,
        retry_delay_seconds=10,
    )
    assert decision.action == "fail"
    assert decision.retry_at is None
