import pytest

from download_types import (
    DownloadResult,
    ErrorCode,
    RetryClass,
    SourceRef,
    TaskRequest,
    TaskSnapshot,
    TaskStatus,
)


def test_request_namespaces_and_legacy_source_id_spelling():
    request = TaskRequest(
        "https://example.test/video",
        source_id="42",
        metadata={"sender": "user@example.test"},
    )
    assert request.source == SourceRef("generic", "42")
    assert request.source_id == "42"
    assert request.source_kind == "generic"


def test_request_rejects_secret_like_metadata():
    with pytest.raises(ValueError):
        TaskRequest("https://example.test/video", SourceRef("qq", "42"), {"cookie": "secret"})


def test_result_normalizes_old_mapping_and_partial_fields():
    result = DownloadResult.from_mapping(
        {
            "success": True,
            "files": ["a.mp4"],
            "failed_items": ["b.mp4: timeout"],
            "retryable": True,
        }
    )
    assert result.partial is True
    assert result.file_count == 1
    assert result.failed_count == 1
    assert result.retry_class == RetryClass.TRANSIENT


def test_snapshot_maps_lease_and_old_partial_result():
    snapshot = TaskSnapshot.from_record(
        {
            "id": 7,
            "original_url": "fake:7",
            "platform": "fake",
            "status": "leased",
            "attempts": 1,
            "result": {"success": True, "partial": True, "files": ["a"]},
            "source_kind": "qq",
            "external_source_id": "7",
        }
    )
    assert snapshot.status == TaskStatus.RUNNING
    assert snapshot.result is not None
    assert snapshot.source == SourceRef("qq", "7")


def test_unknown_error_mapping_is_stable():
    result = DownloadResult.from_mapping({"success": False, "error": "bad"})
    assert result.error_code == ErrorCode.UNKNOWN
    assert result.retryable is False
