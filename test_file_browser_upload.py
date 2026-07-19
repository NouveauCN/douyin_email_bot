"""Focused tests for progressive file-browser uploads."""

import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import file_browser
from media_processor import EdgeCrop, ProcessResult


_TEST_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class UploadFormTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.download_dir = Path(self.tempdir.name)
        self.download_patch = patch.object(
            file_browser, "_DOWNLOAD_DIR", self.download_dir
        )
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

    def test_index_contains_progressive_multipart_form(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="uploadForm"', page)
        self.assertIn('enctype="multipart/form-data"', page)
        self.assertIn('for="uploadInput"', page)
        self.assertIn("multiple", page)
        self.assertNotIn("uploadInput').click()", page)

    def test_enhanced_mobile_upload_returns_json(self):
        response = self.client.post(
            "/api/upload",
            data={"file": (io.BytesIO(_TEST_PNG), "mobile.png")},
            headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 15; Mobile) Chrome/138",
                "X-Requested-With": "XMLHttpRequest",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(len(list((self.download_dir / "slides").glob("*.png"))), 1)

    def test_native_form_upload_redirects_to_status_page(self):
        response = self.client.post(
            "/api/upload",
            data={"file": (io.BytesIO(_TEST_PNG), "fallback.png")},
        )

        self.assertEqual(response.status_code, 303)
        self.assertIn("upload_success=", response.headers["Location"])
        followup = self.client.get(response.headers["Location"])
        self.assertIn("上传成功", followup.get_data(as_text=True))

    def test_native_form_validation_error_redirects(self):
        response = self.client.post("/api/upload", data={})

        self.assertEqual(response.status_code, 303)
        self.assertIn("upload_error=", response.headers["Location"])

    def test_enhanced_batch_upload_returns_summary(self):
        response = self.client.post(
            "/api/upload",
            data={
                "file": [
                    (io.BytesIO(_TEST_PNG), "first.png"),
                    (io.BytesIO(_TEST_PNG), "second.png"),
                ]
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["file_count"], 2)
        self.assertEqual(payload["success_count"], 2)
        self.assertEqual(payload["failed_count"], 0)
        self.assertEqual(len(list((self.download_dir / "slides").glob("*.png"))), 2)

    def test_enhanced_batch_upload_reports_partial_failure(self):
        response = self.client.post(
            "/api/upload",
            data={
                "file": [
                    (io.BytesIO(_TEST_PNG), "valid.png"),
                    (io.BytesIO(b"not allowed"), "invalid.txt"),
                ]
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        payload = response.get_json()
        self.assertEqual(response.status_code, 207)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["success_count"], 1)
        self.assertEqual(payload["failed_count"], 1)
        self.assertIn("invalid.txt", payload["error"])

    def test_native_batch_upload_redirects_with_summary(self):
        response = self.client.post(
            "/api/upload",
            data={
                "file": [
                    (io.BytesIO(_TEST_PNG), "native-first.png"),
                    (io.BytesIO(_TEST_PNG), "native-second.png"),
                ]
            },
        )

        self.assertEqual(response.status_code, 303)
        followup = self.client.get(response.headers["Location"])
        page = followup.get_data(as_text=True)
        self.assertIn("成功上传 2/2 个文件", page)

    def test_video_page_exposes_crop_review_action(self):
        video_dir = self.download_dir / "author"
        video_dir.mkdir()
        (video_dir / "sample.mp4").write_bytes(b"video")

        response = self.client.get("/video/author/sample.mp4")

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="cropBtn"', response.get_data(as_text=True))
        self.assertIn("/api/crop/preview", response.get_data(as_text=True))

    def test_crop_preview_returns_manual_review_candidate(self):
        video_dir = self.download_dir / "author"
        video_dir.mkdir()
        path = video_dir / "sample.mp4"
        path.write_bytes(b"video")
        result = ProcessResult(
            path,
            False,
            EdgeCrop(top=300, bottom=300),
            (1080, 2000),
            (1080, 1400),
            "large video crop requires review",
            requires_review=True,
            confidence="review",
        )

        with patch.object(
            file_browser, "process_media", return_value=result
        ) as process:
            response = self.client.post(
                "/api/crop/preview", json={"path": "author/sample.mp4"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["candidate"])
        self.assertTrue(response.get_json()["requires_review"])
        process.assert_called_once_with(path, dry_run=True)

    def test_crop_apply_passes_explicit_manual_confirmation(self):
        video_dir = self.download_dir / "author"
        video_dir.mkdir()
        path = video_dir / "sample.mp4"
        path.write_bytes(b"video")
        result = ProcessResult(
            path,
            True,
            EdgeCrop(top=300, bottom=300),
            (1080, 2000),
            (1080, 1400),
            "cropped",
            confidence="manual",
        )

        with patch.object(
            file_browser, "process_media", return_value=result
        ) as process:
            response = self.client.post(
                "/api/crop/apply",
                json={"path": "author/sample.mp4", "force_review": True},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["changed"])
        process.assert_called_once_with(path, force_review=True)

    def test_crop_api_rejects_path_traversal(self):
        response = self.client.post(
            "/api/crop/preview", json={"path": "../../outside.mp4"}
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
