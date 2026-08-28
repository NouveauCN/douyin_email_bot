from download_task_service import DownloaderRegistry, DownloadExecutor
from download_types import ErrorCode, RetryClass


class FakeDownloader:
    def __init__(self, prefix="fake:"):
        self.prefix = prefix
        self.calls = []

    def matches(self, url):
        return url.startswith(self.prefix)

    def download(self, url):
        self.calls.append(url)
        return {"success": True, "filepath": "/tmp/result.mp4", "files": ["/tmp/result.mp4"]}


def test_registry_routes_and_rejects_unknown_urls():
    fake = FakeDownloader()
    registry = DownloaderRegistry()
    registry.register("fake", fake)
    executor = DownloadExecutor(registry)
    result = executor.execute("fake:1")
    assert result.success is True
    assert fake.calls == ["fake:1"]

    unknown = executor.execute("other:1")
    assert unknown.success is False
    assert unknown.error_code == ErrorCode.UNSUPPORTED_URL
    assert unknown.retry_class == RetryClass.PERMANENT


def test_registry_duplicate_registration_requires_explicit_replace():
    registry = DownloaderRegistry()
    registry.register("fake", FakeDownloader())
    try:
        registry.register("fake", FakeDownloader())
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate registration must fail")
