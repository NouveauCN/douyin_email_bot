import json

from failure_flag import FailureFlagStore, ProcessRequestStore, failure_flag_key


def test_failure_flag_round_trip_and_clear(tmp_path):
    path = tmp_path / "state" / "flags.json"
    store = FailureFlagStore(path)

    assert store.set_failure(
        sender="sender@example.com",
        url="https://example.invalid/video",
        platform="douyin",
        subject="下载",
        error="DOUYIN_COOKIE=secret timeout",
        attempts=2,
        retry_status="queued",
    )

    data = json.loads(path.read_text(encoding="utf-8"))
    record = data[
        failure_flag_key("sender@example.com", "https://example.invalid/video")
    ]
    assert record["platform"] == "douyin"
    assert "secret" not in record["error"]
    assert record["attempts"] == 2
    assert record["updated_at"]

    assert store.clear("sender@example.com", "https://example.invalid/video")
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_process_request_atomic_roundtrip_and_filters(tmp_path):
    store = ProcessRequestStore(tmp_path / "requests")

    first = store.create_request(
        sender="one@example.com",
        url="https://example.invalid/one",
        subject="处理失败",
    )
    second = store.create_request(sender="two@example.com")

    assert first != second
    assert first.exists() and second.exists()
    assert not list((tmp_path / "requests").glob("*.tmp"))
    assert store.list_requests(sender="one@example.com")[0][1]["url"] == (
        "https://example.invalid/one"
    )
    assert store.list_requests(url="https://example.invalid/one")[0][0] == first
    assert store.list_requests(sender="missing@example.com") == []

    assert store.delete_request(first)
    assert store.list_requests(url="https://example.invalid/one") == []
