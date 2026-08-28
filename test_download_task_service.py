import threading
import time

from download_task_service import DownloaderRegistry, DownloadExecutor, DownloadTaskService
from download_types import SourceRef, TaskRequest, TaskStatus
from task_store import TaskStore


class FakeDownloader:
    def matches(self, url):
        return url.startswith("fake:")

    def download(self, url):
        return {"success": True, "filepath": "/tmp/result.mp4", "files": ["/tmp/result.mp4"]}


def make_service(tmp_path, *, max_attempts=1):
    registry = DownloaderRegistry()
    registry.register("fake", FakeDownloader())
    return DownloadTaskService(
        TaskStore(tmp_path / "state.sqlite"),
        DownloadExecutor(registry),
        worker_count=1,
        max_attempts=max_attempts,
        heartbeat_seconds=1,
        retry_delay_seconds=0,
    )


def test_service_executes_and_isolates_callback_errors(tmp_path):
    service = make_service(tmp_path)
    done = threading.Event()
    submitted = service.submit(
        TaskRequest("fake:1", SourceRef("qq", "1")),
        on_update=lambda _snapshot: (done.set(), (_ for _ in ()).throw(RuntimeError("callback")))[1],
    )
    service.start()
    assert done.wait(2)
    snapshot = service.get(submitted.task_id)
    assert snapshot is not None
    assert snapshot.status == TaskStatus.SUCCEEDED
    service.shutdown()
    service.store.close()


def test_service_unknown_url_fails_without_retry(tmp_path):
    service = make_service(tmp_path)
    submitted = service.submit(TaskRequest("other:1", SourceRef("qq", "1")))
    service.start()
    for _ in range(200):
        snapshot = service.get(submitted.task_id)
        if snapshot and snapshot.status == TaskStatus.FAILED:
            break
        time.sleep(0.01)
    assert snapshot is not None
    assert snapshot.status == TaskStatus.FAILED
    assert snapshot.attempts == 1
    service.shutdown()
    service.store.close()
