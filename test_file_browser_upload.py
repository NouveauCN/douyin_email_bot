"""Focused tests for progressive file-browser uploads."""

import base64
import io
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from subprocess import CompletedProcess
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
        self.manifest_patch = patch.object(file_browser, "_DEDUP_MANIFEST", {})
        self.pending_patch = patch.object(file_browser, "_PENDING_DUPS", [])
        self.resolved_patch = patch.object(file_browser, "_DEDUP_RESOLVED_PAIRS", set())
        self.state_patch = patch.object(
            file_browser,
            "_DEDUP_STATE_FILE",
            Path(self.tempdir.name) / "browser_cache" / "dedup_state.json",
        )
        self.index_patch.start()
        self.manifest_patch.start()
        self.pending_patch.start()
        self.resolved_patch.start()
        self.state_patch.start()
        self.client = file_browser.app.test_client()
        self.client.environ_base["HTTP_ORIGIN"] = "http://localhost"

    def tearDown(self):
        self.state_patch.stop()
        self.resolved_patch.stop()
        self.pending_patch.stop()
        self.manifest_patch.stop()
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

    def test_index_places_final_mobile_overflow_guard_after_component_rules(self):
        page = self.client.get("/").get_data(as_text=True)

        marker = "/* Mobile overflow guard: this block intentionally follows all component rules. */"
        marker_pos = page.index(marker)
        self.assertGreater(marker_pos, page.index(".upload-form {\n    margin-bottom:"))
        self.assertGreater(marker_pos, page.index(".browse-search { display:flex;"))
        guard = page[marker_pos:]
        self.assertIn("html, body { max-width:100%; min-width:0; overflow-x:hidden; }", guard)
        self.assertIn(".upload-form { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); }", guard)
        self.assertIn(".browse-search { display:grid; grid-template-columns:minmax(0,1fr); }", guard)
        self.assertIn(".browse-search input { min-width:0; }", guard)

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
        # The pending-duplicates section must start expanded so an automatic
        # download duplicate is visible without an extra click.
        self.assertIn("header.className = 'section-header';", page)
        self.assertNotIn("header.className = 'section-header collapsed';", page)
        self.assertIn("body.className = 'collapsible-body dup-section';", page)
        self.assertIn("setInterval(loadDups, 15000);", page)

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

    def test_comics_upload_creates_scoped_duplicate_candidate(self):
        first = self.client.post(
            "/api/upload",
            data={"target": "comics", "file": (io.BytesIO(_TEST_PNG), "first.png")},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        second = self.client.post(
            "/api/upload",
            data={"target": "comics", "file": (io.BytesIO(_TEST_PNG), "second.png")},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        self.assertTrue(first.get_json()["success"])
        duplicate = second.get_json()["duplicate"]
        self.assertEqual(second.status_code, 200)
        self.assertEqual(duplicate["duplicate_of"].count("comics:"), 0)
        pending = self.client.get("/api/dups").get_json()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["root"], "comics")
        self.assertTrue(pending[0]["new_file"]["raw_url"].startswith("/comics/raw/"))

    def test_comics_duplicate_keep_and_delete_are_root_scoped(self):
        for name in ("first.png", "second.png"):
            self.client.post(
                "/api/upload",
                data={"target": "comics", "file": (io.BytesIO(_TEST_PNG), name)},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        pending = self.client.get("/api/dups").get_json()[0]
        new_path = pending["new_file"]["relpath"]
        old_path = pending["match_file"]["relpath"]

        keep = self.client.post("/api/dup/keep", json={"root": "comics", "path": new_path})
        self.assertEqual(keep.status_code, 200)
        self.assertTrue(keep.get_json()["success"])

        # Recreate a candidate and verify deleting the old side is confined to comics.
        file_browser._PENDING_DUPS.append({
            "root": "comics", "new_file": new_path, "match_file": old_path,
            "dhash_dist": 0, "mse": 0.0, "similarity_pct": 100,
        })
        delete = self.client.post("/api/dup/delete", json={"root": "comics", "path": old_path})
        self.assertEqual(delete.status_code, 200)
        self.assertFalse((self.comics_dir / old_path).exists())
        self.assertTrue((self.comics_dir / new_path).exists())

    def test_comics_files_are_not_crop_targets(self):
        self.comics_dir.mkdir(parents=True)
        image = self.comics_dir / "anime.png"
        image.write_bytes(_TEST_PNG)
        response = self.client.post("/api/crop/preview", json={"path": "../original-comics/anime.png"})
        self.assertEqual(response.status_code, 403)

    def test_comics_dup_actions_reject_invalid_root_and_traversal(self):
        invalid_root = self.client.post(
            "/api/dup/keep", json={"root": "downloads", "path": "../outside.png"}
        )
        self.assertEqual(invalid_root.status_code, 403)
        invalid_namespace = self.client.post(
            "/api/dup/keep", json={"root": "other", "path": "anime.png"}
        )
        self.assertEqual(invalid_namespace.status_code, 400)

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


class DedupRefreshTests(unittest.TestCase):
    """Downloaded files must join the dedup index even after startup."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.download_dir = Path(self.tempdir.name)
        self._patches = [
            patch.object(file_browser, "_DOWNLOAD_DIR", self.download_dir),
            patch.object(file_browser, "_COMICS_DIR", self.download_dir / "comics"),
            patch.object(file_browser, "_DEDUP_INDEX", {}),
            patch.object(file_browser, "_DEDUP_MANIFEST", {}),
            patch.object(file_browser, "_PENDING_DUPS", []),
            patch.object(file_browser, "_DEDUP_RESOLVED_PAIRS", set()),
            patch.object(
                file_browser,
                "_DEDUP_STATE_FILE",
                Path(self.tempdir.name) / "browser_cache" / "dedup_state.json",
            ),
        ]
        for patcher in self._patches:
            patcher.start()
        self.client = file_browser.app.test_client()
        self.client.environ_base["HTTP_ORIGIN"] = "http://localhost"

    def tearDown(self):
        for patcher in reversed(self._patches):
            patcher.stop()
        self.tempdir.cleanup()

    def _write(self, rel: str, data: bytes) -> Path:
        path = self.download_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_build_indexes_nested_and_flat_media(self):
        self._write("slides/flat.png", _TEST_PNG)
        self._write("bilibili/作者/nested.png", _TEST_PNG)
        self._write(".hidden/hidden.png", _TEST_PNG)

        file_browser._build_dedup_index()

        keys = set(file_browser._DEDUP_INDEX)
        self.assertIn("slides/flat.png", keys)
        self.assertIn("bilibili/作者/nested.png", keys)
        self.assertFalse(any("hidden" in key for key in keys))
        self.assertIn("bilibili/作者/nested.png", file_browser._DEDUP_MANIFEST)

    def test_refresh_picks_up_new_files_and_drops_deleted_ones(self):
        flat = self._write("slides/flat.png", _TEST_PNG)
        file_browser._build_dedup_index()

        self._write("slides/added.png", _TEST_PNG)
        with file_browser._DEDUP_LOCK:
            file_browser._refresh_download_dedup_index(flag_duplicates=False)
        self.assertIn("slides/added.png", file_browser._DEDUP_INDEX)

        flat.unlink()
        with file_browser._DEDUP_LOCK:
            file_browser._refresh_download_dedup_index(flag_duplicates=False)
        self.assertNotIn("slides/flat.png", file_browser._DEDUP_INDEX)
        self.assertNotIn("slides/flat.png", file_browser._DEDUP_MANIFEST)

    def test_new_download_matching_indexed_file_becomes_pending(self):
        self._write("slides/base.png", _TEST_PNG)
        file_browser._build_dedup_index()

        # Simulates the bot dropping a duplicate after startup.
        new_rel = "季风的学长/20260925_235959_BV1xpbj6YEqk.png"
        self._write(new_rel, _TEST_PNG)
        with file_browser._DEDUP_LOCK:
            file_browser._refresh_download_dedup_index(flag_duplicates=True)

        self.assertEqual(len(file_browser._PENDING_DUPS), 1)
        pending = file_browser._PENDING_DUPS[0]
        self.assertEqual(pending["new_file"], new_rel)
        self.assertEqual(pending["match_file"], "slides/base.png")
        self.assertNotIn(new_rel, file_browser._DEDUP_INDEX)

        # The periodic worker must not append the same pending entry again.
        with file_browser._DEDUP_LOCK:
            file_browser._refresh_download_dedup_index(flag_duplicates=True)
        self.assertEqual(len(file_browser._PENDING_DUPS), 1)

    def test_startup_build_keeps_historical_duplicates_clean(self):
        self._write("slides/a.png", _TEST_PNG)
        self._write("slides/b.png", _TEST_PNG)

        file_browser._build_dedup_index()

        self.assertEqual(file_browser._PENDING_DUPS, [])
        self.assertIn("slides/a.png", file_browser._DEDUP_INDEX)
        self.assertIn("slides/b.png", file_browser._DEDUP_INDEX)

    def test_similarity_keeps_one_decimal(self):
        self.assertEqual(file_browser._similarity_from_mse(0.0), 100.0)
        self.assertEqual(file_browser._similarity_from_mse(0.5), 99.0)
        self.assertEqual(file_browser._similarity_from_mse(25.0), 50.0)

    def test_match_fingerprints_reports_worst_frame(self):
        good = (0b1010, bytes([10]) * 1024)
        worse = (0b1010, bytes([16]) * 1024)  # mse 36 — passes but imperfect
        left = [good, good]
        right = [good, worse]

        result = file_browser._match_fingerprints(left, right)

        self.assertIsNotNone(result)
        dist, mse_val, pct = result
        self.assertEqual(dist, 0)
        self.assertEqual(mse_val, file_browser._mse(good[1], worse[1]))
        self.assertEqual(pct, file_browser._similarity_from_mse(mse_val))
        self.assertLess(pct, 100.0)
        # Any pair beyond threshold rejects the whole match.
        broken = (0, bytes([250]) * 1024)
        self.assertIsNone(file_browser._match_fingerprints([good], [broken]))

    def test_video_never_matches_image(self):
        image_fingerprint = file_browser._compute_media_fingerprint(
            self._write("slides/solo.png", _TEST_PNG)
        )
        # Same bytes, but stored under a video key: kind must block it.
        file_browser._DEDUP_INDEX["某作者/solo.mp4"] = list(image_fingerprint)
        file_browser._DEDUP_INDEX["slides/solo.png"] = list(image_fingerprint)
        file_browser._DEDUP_MANIFEST.update({
            "某作者/solo.mp4": (1, 1),
            "slides/solo.png": (2, 2),
        })

        scan = self.client.post("/api/dups/scan", json={})

        self.assertEqual(scan.get_json()["added"], 0)
        self.assertEqual(file_browser._PENDING_DUPS, [])

        upload = self.client.post(
            "/api/upload",
            data={"target": "downloads", "file": (io.BytesIO(_TEST_PNG), "copy.png")},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        # The video key is listed first; a missing kind check would match it.
        self.assertEqual(
            upload.get_json()["duplicate"]["duplicate_of"], "slides/solo.png"
        )

    def test_video_fingerprint_samples_five_frames(self):
        from io import BytesIO as _BytesIO

        from PIL import Image as _Image

        buffer = _BytesIO()
        _Image.new("RGB", (64, 64), (200, 100, 50)).save(buffer, "JPEG")
        fake = CompletedProcess(args=[], returncode=0, stdout=buffer.getvalue(), stderr=b"")

        with (
            patch.object(file_browser, "_video_duration", return_value=10.0),
            patch.object(file_browser.subprocess, "run", return_value=fake) as run,
        ):
            frames = file_browser._compute_media_fingerprint(Path("clip.mp4"))

        self.assertEqual(run.call_count, 5)
        self.assertEqual(len(frames), 5)
        expected_img = _Image.open(_BytesIO(buffer.getvalue())).convert("RGB")
        expected = (file_browser._compute_dhash(expected_img),
                    file_browser._compute_thumbnail(expected_img))
        self.assertTrue(all(frame == expected for frame in frames))

    def test_v1_state_wraps_images_and_recomputes_videos(self):
        state = {
            "version": 1,
            "fingerprint_version": 1,
            "index": {
                "slides/old.png": [12345, base64.b64encode(b"x" * 1024).decode()],
                "作者/old.mp4": [54321, base64.b64encode(b"y" * 1024).decode()],
            },
            "manifest": {
                "slides/old.png": [111, 222],
                "作者/old.mp4": [333, 444],
            },
            "pending": [
                {"root": "downloads", "new_file": "a.mp4", "match_file": "b.png",
                 "dhash_dist": 0, "mse": 0.0, "similarity_pct": 100},
            ],
            "resolved_pairs": [],
        }
        state_path = file_browser._DEDUP_STATE_FILE
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state), encoding="utf-8")

        file_browser._load_dedup_state()

        self.assertEqual(
            file_browser._DEDUP_INDEX["slides/old.png"],
            [(12345, b"x" * 1024)],
        )
        self.assertNotIn("作者/old.mp4", file_browser._DEDUP_INDEX)
        self.assertNotIn("作者/old.mp4", file_browser._DEDUP_MANIFEST)
        self.assertIn("slides/old.png", file_browser._DEDUP_MANIFEST)
        self.assertEqual(file_browser._PENDING_DUPS, [])

    def test_scan_flags_existing_identical_trio(self):
        self._write("slides/a.png", _TEST_PNG)
        self._write("slides/b.png", _TEST_PNG)
        self._write("slides/c.png", _TEST_PNG)
        file_browser._build_dedup_index()
        self.assertEqual(file_browser._PENDING_DUPS, [])

        response = self.client.post("/api/dups/scan", json={})

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["added"], 3)
        self.assertEqual(len(file_browser._PENDING_DUPS), 3)

        # A second scan must not duplicate existing pendings.
        again = self.client.post("/api/dups/scan", json={})
        self.assertEqual(again.get_json()["added"], 0)

    def test_keep_both_is_not_reflaged_by_scan(self):
        self._write("slides/x.png", _TEST_PNG)
        file_browser._build_dedup_index()
        self._write("slides/y.png", _TEST_PNG)
        with file_browser._DEDUP_LOCK:
            file_browser._refresh_download_dedup_index(flag_duplicates=True)
        self.assertEqual(len(file_browser._PENDING_DUPS), 1)

        keep = self.client.post(
            "/api/dup/keep", json={"path": "slides/y.png", "root": "downloads"}
        )
        self.assertEqual(keep.get_json()["success"], True)
        self.assertEqual(file_browser._PENDING_DUPS, [])

        scan = self.client.post("/api/dups/scan", json={})
        self.assertEqual(scan.get_json()["added"], 0)
        self.assertEqual(file_browser._PENDING_DUPS, [])

    def test_startup_skips_unchanged_fingerprints(self):
        self._write("comics/img.png", _TEST_PNG)
        self._write("slides/a.png", _TEST_PNG)
        file_browser._build_dedup_index()

        original = file_browser._media_to_image
        with patch.object(file_browser, "_media_to_image", wraps=original) as spy:
            file_browser._build_dedup_index()

        spy.assert_not_called()

    def test_dup_delete_removes_paired_backup(self):
        self._write("slides/old.png", _TEST_PNG)
        file_browser._build_dedup_index()
        self._write("slides/new.png", _TEST_PNG)
        self._write("slides/new_original.bak", b"bak")
        with file_browser._DEDUP_LOCK:
            file_browser._refresh_download_dedup_index(flag_duplicates=True)
        self.assertEqual(len(file_browser._PENDING_DUPS), 1)

        response = self.client.post(
            "/api/dup/delete", json={"path": "slides/new.png"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertFalse((self.download_dir / "slides/new.png").exists())
        self.assertFalse((self.download_dir / "slides/new_original.bak").exists())
        self.assertTrue((self.download_dir / "slides/old.png").exists())
        self.assertEqual(file_browser._PENDING_DUPS, [])

    def test_file_delete_removes_paired_backup(self):
        self._write("作者/x.mp4", b"video")
        self._write("作者/x_original.bak", b"bak")

        response = self.client.post("/api/delete", json={"path": "作者/x.mp4"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertFalse((self.download_dir / "作者/x.mp4").exists())
        self.assertFalse((self.download_dir / "作者/x_original.bak").exists())

    def test_dedup_state_persists_and_reloads(self):
        self._write("slides/keep.png", _TEST_PNG)
        file_browser._build_dedup_index()
        before = file_browser._DEDUP_INDEX["slides/keep.png"]
        file_browser._PENDING_DUPS.append({
            "root": "downloads",
            "new_file": "slides/keep.png",
            "match_file": "slides/other.png",
            "dhash_dist": 0,
            "mse": 0.0,
            "similarity_pct": 100,
        })
        with file_browser._DEDUP_LOCK:
            file_browser._save_dedup_state()
        self.assertTrue(file_browser._DEDUP_STATE_FILE.exists())

        file_browser._DEDUP_INDEX.clear()
        file_browser._DEDUP_MANIFEST.clear()
        file_browser._PENDING_DUPS.clear()

        file_browser._load_dedup_state()

        self.assertEqual(file_browser._DEDUP_INDEX["slides/keep.png"], before)
        self.assertIn("slides/keep.png", file_browser._DEDUP_MANIFEST)
        self.assertEqual(len(file_browser._PENDING_DUPS), 1)
        self.assertEqual(
            file_browser._PENDING_DUPS[0]["match_file"], "slides/other.png"
        )

    def test_upload_flags_duplicate_created_after_index_build(self):
        # Built while empty — simulates a bot download landing after startup.
        file_browser._build_dedup_index()
        self._write("slides/base.png", _TEST_PNG)

        response = self.client.post(
            "/api/upload",
            data={"target": "downloads", "file": (io.BytesIO(_TEST_PNG), "copy.png")},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["duplicate"]["duplicate_of"], "slides/base.png")

    def test_video_card_shows_author(self):
        self._write("某作者/20260102_030405_abc.mp4", b"x")

        html = self.client.get("/").get_data(as_text=True)

        self.assertIn('<span class="stat">某作者</span>', html)

    def test_format_date_only_accepts_real_dates(self):
        self.assertEqual(file_browser._format_date("20260925"), "2026-09-25")
        self.assertEqual(file_browser._format_date("bilibili"), "")
        self.assertEqual(file_browser._format_date("20261301"), "")
        self.assertEqual(file_browser._format_date("20260932"), "")

    def test_poster_card_shows_mtime_date_not_prefix_fragment(self):
        poster = self._write("slides/bilibili_probe-poster.jpg", _TEST_PNG)
        moment = datetime(2026, 9, 25, 12, 0, 0).timestamp()
        os.utime(poster, (moment, moment))

        html = self.client.get("/").get_data(as_text=True)

        self.assertIn('<span class="stat">2026-09-25</span>', html)
        self.assertNotIn("bili-bi-li", html)

    def test_dup_section_links_viewers_and_thumbnails(self):
        page = self.client.get("/").get_data(as_text=True)

        self.assertIn("function dupFileHtml(", page)
        self.assertIn("replace('/raw/', '/thumb/')", page)
        self.assertIn("'/video/' : '/image/'", page)
        self.assertIn('id="dedupScanBtn"', page)
        self.assertIn("'/api/dups/scan'", page)
        dup_source = page.split("function dupFileHtml(")[1].split("function loadDups")[0]
        self.assertNotIn("🎬", dup_source)
        self.assertIn("target=\"_blank\"", dup_source)


if __name__ == "__main__":
    unittest.main()
