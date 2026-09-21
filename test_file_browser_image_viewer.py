"""Focused tests for the embedded file-browser image viewer."""

import base64
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote
from unittest.mock import patch

import file_browser


_TEST_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ImageViewerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.download_dir = Path(self.tempdir.name)
        self.slides_dir = self.download_dir / "slides"
        self.slides_dir.mkdir()
        (self.slides_dir / "01.png").write_bytes(_TEST_PNG)
        (self.slides_dir / "02.png").write_bytes(_TEST_PNG)
        (self.slides_dir / "not-an-image.txt").write_text("ignore me")
        self.download_patch = patch.object(
            file_browser, "_DOWNLOAD_DIR", self.download_dir
        )
        self.comics_dir = self.download_dir / "comics" / "pics"
        self.comics_dir.mkdir(parents=True)
        self.comics_patch = patch.object(file_browser, "_COMICS_DIR", self.comics_dir)
        self.download_patch.start()
        self.comics_patch.start()
        self.client = file_browser.app.test_client()

    def tearDown(self):
        self.download_patch.stop()
        self.comics_patch.stop()
        self.tempdir.cleanup()

    def test_image_page_embeds_viewer_and_starts_at_requested_image(self):
        response = self.client.get("/image/slides/02.png")
        page = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("2 / 2", page)
        self.assertIn("上一张", page)
        self.assertIn("下一张", page)
        self.assertIn("/raw/slides/01.png", page)
        self.assertIn("/raw/slides/02.png", page)
        self.assertIn('const IMAGES = [{', page)

    def test_image_viewer_contains_mobile_safe_controls(self):
        page = self.client.get("/image/slides/02.png").get_data(as_text=True)

        self.assertIn("min-height: min(65svh, 520px)", page)
        self.assertIn(".gallery-wrapper img { max-height: 65svh;", page)
        self.assertIn(".nav-btn { width: 44px; height: 44px;", page)

    def test_home_and_browse_link_to_embedded_viewer(self):
        home = self.client.get("/").get_data(as_text=True)
        browse = self.client.get("/browse/slides").get_data(as_text=True)

        self.assertIn('/image/slides/01.png', home)
        self.assertIn('/image/slides/01.png', browse)
        self.assertIn('download', browse)

    def test_non_image_and_traversal_are_rejected(self):
        self.assertEqual(self.client.get("/image/slides/not-an-image.txt").status_code, 404)
        self.assertEqual(self.client.get("/image/../outside.png").status_code, 403)

    def test_special_filename_is_serialized_safely(self):
        name = "quote-'<script>#.png"
        (self.slides_dir / name).write_bytes(_TEST_PNG)
        response = self.client.get(f"/image/slides/{quote(name)}")
        page = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("quote-", page)
        self.assertIn("\\u003cscript\\u003e", page)

    def test_external_symlink_is_not_served(self):
        outside = Path(self.tempdir.name).parent / f"outside-{Path(self.tempdir.name).name}.png"
        outside.write_bytes(_TEST_PNG)
        link = self.slides_dir / "outside.png"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        try:
            self.assertEqual(self.client.get("/image/slides/outside.png").status_code, 403)
        finally:
            outside.unlink(missing_ok=True)

    def test_comics_gallery_scans_nested_images_and_exposes_delete(self):
        nested = self.comics_dir / "nested"
        nested.mkdir()
        (self.comics_dir / "plain.png").write_bytes(_TEST_PNG)
        (nested / "nested.jpg").write_bytes(_TEST_PNG)
        (self.download_dir / "author").mkdir()
        (self.download_dir / "author" / "sample.mp4").write_bytes(b"video")
        slides = self.download_dir / "slides"
        slides.mkdir(exist_ok=True)
        (slides / "ordinary.png").write_bytes(_TEST_PNG)
        page = self.client.get("/").get_data(as_text=True)
        self.assertLess(page.index("📹 视频"), page.index("🖼️ 图片"))
        self.assertLess(page.index("🖼️ 图片"), page.index("二次元图片"))
        self.assertIn('/comics/image/plain.png', page)
        self.assertIn('/comics/image/nested/nested.jpg', page)
        comics_card = page[page.index('class="card media-card comics-card"'):]
        self.assertIn('class="del-btn"', comics_card)
        self.assertIn("/api/comics/delete", comics_card)

    def test_comics_delete_removes_image_and_empty_nested_parents(self):
        nested = self.comics_dir / "nested" / "deeper"
        nested.mkdir(parents=True)
        image = nested / "delete-me.png"
        image.write_bytes(_TEST_PNG)

        response = self.client.post(
            "/api/comics/delete",
            json={"path": "nested/deeper/delete-me.png"},
            headers={"Origin": "http://localhost"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertFalse(image.exists())
        self.assertFalse((self.comics_dir / "nested" / "deeper").exists())
        self.assertFalse((self.comics_dir / "nested").exists())
        self.assertTrue(self.comics_dir.exists())

    def test_comics_delete_rejects_root_non_images_and_external_symlinks(self):
        (self.comics_dir / "nested").mkdir()
        (self.comics_dir / "nested" / "note.txt").write_text("keep")
        outside = Path(self.tempdir.name).parent / f"outside-delete-{Path(self.tempdir.name).name}.png"
        outside.write_bytes(_TEST_PNG)
        link = self.comics_dir / "outside.png"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            outside.unlink(missing_ok=True)
            self.skipTest(f"symlink unavailable: {exc}")
        try:
            for path, expected in ((".", 403), ("nested", 400), ("nested/note.txt", 400), ("outside.png", 403), ("../outside.png", 403)):
                with self.subTest(path=path):
                    response = self.client.post(
                        "/api/comics/delete",
                        json={"path": path},
                        headers={"Origin": "http://localhost"},
                    )
                    self.assertEqual(response.status_code, expected)
            self.assertTrue(outside.exists())
            self.assertTrue(link.is_symlink())
        finally:
            outside.unlink(missing_ok=True)

    def test_landscape_images_span_two_gallery_columns(self):
        slide = self.slides_dir / "landscape.png"
        file_browser.Image.new("RGB", (2, 1)).save(slide)
        landscape = self.comics_dir / "landscape.png"
        file_browser.Image.new("RGB", (2, 1)).save(landscape)

        page = self.client.get("/").get_data(as_text=True)

        self.assertIn('class="card media-card landscape-card"', page)
        self.assertIn('class="card media-card comics-card landscape-card"', page)
        self.assertIn('.card.landscape-card { grid-column: span 2; }', page)
        self.assertIn('.card.landscape-card .card-thumb { aspect-ratio: 16 / 9; }', page)
        self.assertIn("function markLandscapeCard(image)", page)
        self.assertIn("document.querySelectorAll('.card-thumb')", page)

    def test_landscape_video_thumbnail_keeps_its_orientation(self):
        video = self.download_dir / "landscape.mp4"
        video.write_bytes(b"video")
        completed = file_browser.subprocess.CompletedProcess(
            args=[], returncode=0, stdout="320,180\n"
        )

        with patch.object(file_browser.subprocess, "run", return_value=completed) as run:
            thumbnail_filter = file_browser._video_thumbnail_filter(video)

        self.assertEqual(
            thumbnail_filter,
            "scale=320:180:force_original_aspect_ratio=increase,crop=320:180",
        )
        self.assertIn("ffprobe", run.call_args.args[0])

    def test_comics_raw_and_viewer_are_independent_routes(self):
        nested = self.comics_dir / "nested"
        nested.mkdir()
        image = nested / "nested.png"
        image.write_bytes(_TEST_PNG)

        raw = self.client.get("/comics/raw/nested/nested.png")
        viewer = self.client.get("/comics/image/nested/nested.png")

        self.assertEqual(raw.status_code, 200)
        self.assertEqual(raw.data, _TEST_PNG)
        raw.close()
        self.assertEqual(viewer.status_code, 200)
        self.assertIn("/comics/raw/nested/nested.png", viewer.get_data(as_text=True))
        self.assertIn('href="/"', viewer.get_data(as_text=True))

    def test_comics_rejects_traversal_and_external_symlink(self):
        outside = Path(self.tempdir.name).parent / f"outside-comics-{Path(self.tempdir.name).name}.png"
        outside.write_bytes(_TEST_PNG)
        link = self.comics_dir / "outside.png"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            outside.unlink(missing_ok=True)
            self.skipTest(f"symlink unavailable: {exc}")
        try:
            self.assertEqual(self.client.get("/comics/raw/../outside.png").status_code, 403)
            self.assertEqual(self.client.get("/comics/image/outside.png").status_code, 403)
        finally:
            outside.unlink(missing_ok=True)

    def test_empty_comics_section_is_visible(self):
        page = self.client.get("/").get_data(as_text=True)

        self.assertIn("二次元图片", page)
        self.assertIn("暂无二次元图片", page)


if __name__ == "__main__":
    unittest.main()
