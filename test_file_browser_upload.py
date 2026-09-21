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
        self.comics_dir = self.download_dir / "original-comics"
        self.comics_patch = patch.object(file_browser, "_COMICS_DIR", self.comics_dir)
        self.comics_patch.start()
        self.index_patch = patch.object(file_browser, "_DEDUP_INDEX", {})
        self.pending_patch = patch.object(file_browser, "_PENDING_DUPS", [])
        self.index_patch.start()
        self.pending_patch.start()
        self.client = file_browser.app.test_client()
        self.client.environ_base["HTTP_ORIGIN"] = "http://localhost"

    def tearDown(self):
        self.pending_patch.stop()
        self.index_patch.stop()
        self.download_patch.stop()
        self.comics_patch.stop()
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
        self.assertIn('name="target"', page)
        self.assertIn("二次元（仅图片）", page)

    def test_index_contains_mobile_layout_rules(self):
        page = self.client.get("/").get_data(as_text=True)

        self.assertIn("env(safe-area-inset-left)", page)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", page)
        self.assertIn(".card.landscape-card { grid-column: span 1; }", page)
        self.assertIn("@media (max-width:340px)", page)
        self.assertIn(".upload-form { display: grid;", page)
        self.assertIn(".browse-search { display: grid;", page)
        self.assertIn(".top-tabs { display: grid; grid-template-columns: repeat(3", page)

    def test_index_exposes_manual_regex_search_and_helpers(self):
        page = self.client.get("/").get_data(as_text=True)

        self.assertIn('id="searchMode"', page)
        self.assertIn('value="regex"', page)
        self.assertIn('id="searchHelper"', page)
        self.assertIn("图片扩展名", page)
        self.assertIn("视频扩展名", page)
        self.assertIn("日期时间前缀", page)
        self.assertIn('id="searchClear"', page)
        self.assertIn("function createSearchMatcher(input, mode)", page)
        self.assertIn("new RegExp(pattern, 'i')", page)
        self.assertIn("正则表达式无效：", page)
        self.assertIn("input.setAttribute('aria-invalid', 'true')", page)

    def test_empty_home_exposes_comics_gallery(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("二次元图片", page)
        self.assertIn("暂无二次元图片", page)
        self.assertIn('class="section-header collapsed"', page)
        self.assertNotIn("external-section-link", page)

    def test_home_local_sections_start_collapsed(self):
        (self.download_dir / "author").mkdir()
        (self.download_dir / "author" / "sample.mp4").write_bytes(b"video")
        (self.download_dir / "slides").mkdir()
        (self.download_dir / "slides" / "sample.png").write_bytes(_TEST_PNG)

        page = self.client.get("/").get_data(as_text=True)

        self.assertIn(
            '<div class="section-header collapsed" data-section="videos" onclick="toggleSection(this)"'
            ' title="点击折叠/展开">\n    <span class="arrow">▼</span> 📹 视频',
            page,
        )
        self.assertIn(
            '<div class="section-header collapsed" data-section="images" onclick="toggleSection(this)"'
            ' title="点击折叠/展开"\n       style="margin-top:10px">\n'
            '    <span class="arrow">▼</span> 🖼️ 图片',
            page,
        )
        self.assertEqual(page.count('class="collapsible-body card-grid collapsed"'), 3)
        self.assertIn('id="mediaSearch"', page)
        self.assertIn('function updateSearch()', page)
        self.assertIn('data-search="sample.mp4 author/sample.mp4 author"', page)
        self.assertIn("header.className = 'section-header collapsed';", page)
        self.assertIn(
            "body.className = 'collapsible-body dup-section collapsed';", page
        )

    def test_delete_keeps_section_state_without_full_page_reload(self):
        (self.download_dir / "author").mkdir()
        (self.download_dir / "author" / "sample.mp4").write_bytes(b"video")
        (self.download_dir / "slides").mkdir()
        (self.download_dir / "slides" / "sample.png").write_bytes(_TEST_PNG)
        page = self.client.get("/").get_data(as_text=True)
        start = page.index("function confirmDelete")
        end = page.index("function setUploadStatus")
        delete_script = page[start:end]

        self.assertIn("removeDeletedCard", delete_script)
        self.assertNotIn("location.reload()", delete_script)
        self.assertIn("function reloadPreservingSections()", page)
        self.assertIn('data-section="videos"', page)
        self.assertIn('data-section="images"', page)
        self.assertIn('data-section="comics"', page)

    def test_comics_gallery_uses_same_card_grid_and_search_metadata(self):
        self.comics_dir.mkdir()
        (self.comics_dir / "artist").mkdir()
        (self.comics_dir / "artist" / "hero.png").write_bytes(_TEST_PNG)

        page = self.client.get("/").get_data(as_text=True)

        self.assertIn('data-section="comics"', page)
        self.assertIn('class="collapsible-body card-grid collapsed"', page)
        self.assertIn('class="card media-card comics-card', page)
        self.assertIn('data-search="hero.png artist/hero.png"', page)
        self.assertNotIn('class="collapsible-body collapsed"', page)

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

    def test_comics_upload_writes_original_comics_directory(self):
        response = self.client.post(
            "/api/upload",
            data={
                "target": "comics",
                "file": (io.BytesIO(_TEST_PNG), "anime.png"),
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(len(list(self.comics_dir.glob("*.png"))), 1)
        self.assertFalse((self.download_dir / "slides").exists())

    def test_comics_upload_rejects_video_and_invalid_target(self):
        video = self.client.post(
            "/api/upload",
            data={
                "target": "comics",
                "file": (io.BytesIO(b"video"), "anime.mp4"),
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        invalid = self.client.post(
            "/api/upload",
            data={
                "target": "elsewhere",
                "file": (io.BytesIO(_TEST_PNG), "anime.png"),
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        self.assertEqual(video.status_code, 400)
        self.assertIn("仅支持图片", video.get_json()["error"])
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("无效", invalid.get_json()["error"])

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
        process.assert_called_once_with(
            path, dry_run=True, lock_root=self.download_dir
        )

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
        process.assert_called_once_with(
            path, force_review=True, lock_root=self.download_dir
        )

    def test_crop_api_rejects_path_traversal(self):
        response = self.client.post(
            "/api/crop/preview", json={"path": "../../outside.mp4"}
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
