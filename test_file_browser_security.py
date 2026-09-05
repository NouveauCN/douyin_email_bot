"""Security and resource-limit tests for the unauthenticated file browser."""

import base64
import io
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import file_browser


_TEST_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FileBrowserSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.download_dir = Path(self.tempdir.name)
        self.download_patch = patch.object(file_browser, "_DOWNLOAD_DIR", self.download_dir)
        self.download_patch.start()
        self.index_patch = patch.object(file_browser, "_DEDUP_INDEX", {})
        self.pending_patch = patch.object(file_browser, "_PENDING_DUPS", [])
        self.index_patch.start()
        self.pending_patch.start()
        self.client = file_browser.app.test_client()

    def tearDown(self):
        self.pending_patch.stop()
        self.index_patch.stop()
        self.download_patch.stop()
        self.tempdir.cleanup()

    def test_mutating_request_requires_matching_source(self):
        missing = self.client.post("/api/delete", json={"path": "missing"})
        cross_origin = self.client.post(
            "/api/delete",
            json={"path": "missing"},
            headers={"Origin": "https://attacker.example"},
        )

        self.assertEqual(missing.status_code, 403)
        self.assertEqual(cross_origin.status_code, 403)

    def test_origin_takes_priority_and_referer_can_supply_same_origin(self):
        origin_wins = self.client.post(
            "/api/delete",
            json={"path": "missing"},
            headers={
                "Origin": "https://attacker.example",
                "Referer": "http://localhost/page",
            },
        )
        referer_fallback = self.client.post(
            "/api/delete",
            json={"path": "missing"},
            headers={"Referer": "http://localhost/page"},
        )

        self.assertEqual(origin_wins.status_code, 403)
        self.assertEqual(referer_fallback.status_code, 404)

    def test_same_origin_upload_succeeds(self):
        response = self.client.post(
            "/api/upload",
            data={"file": (io.BytesIO(_TEST_PNG), "same-origin.png")},
            headers={
                "Origin": "http://localhost",
                "X-Requested-With": "XMLHttpRequest",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])

    def test_upload_file_count_limit_returns_413(self):
        with patch.object(file_browser, "_MAX_UPLOAD_FILES", 1):
            response = self.client.post(
                "/api/upload",
                data={
                    "file": [
                        (io.BytesIO(_TEST_PNG), "one.png"),
                        (io.BytesIO(_TEST_PNG), "two.png"),
                    ]
                },
                headers={
                    "Origin": "http://localhost",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )

        self.assertEqual(response.status_code, 413)
        self.assertFalse(response.get_json()["success"])

    def test_upload_content_length_limit_returns_413(self):
        with patch.dict(file_browser.app.config, {"MAX_CONTENT_LENGTH": 1}):
            response = self.client.post(
                "/api/upload",
                data={"file": (io.BytesIO(_TEST_PNG), "too-large.png")},
                headers={
                    "Origin": "http://localhost",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )

        self.assertEqual(response.status_code, 413)

    def test_busy_media_semaphore_rejects_upload(self):
        semaphore = threading.BoundedSemaphore(1)
        semaphore.acquire()
        with patch.object(file_browser, "_MEDIA_SEMAPHORE", semaphore):
            response = self.client.post(
                "/api/upload",
                data={"file": (io.BytesIO(_TEST_PNG), "busy.png")},
                headers={
                    "Origin": "http://localhost",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
        semaphore.release()

        self.assertEqual(response.status_code, 429)
        self.assertFalse(response.get_json()["success"])


if __name__ == "__main__":
    unittest.main()


def test_entrypoint_disables_flask_dotenv_loading(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "argv", ["file_browser.py"])
    monkeypatch.setattr(file_browser, "_build_dedup_index", lambda: None)

    def fake_run(**kwargs):
        calls.append(kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(file_browser.app, "run", fake_run)

    file_browser.main()

    assert calls[0]["load_dotenv"] is False
