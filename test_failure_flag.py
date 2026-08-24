import json

from failure_flag import FailureFlagStore, failure_flag_key


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
