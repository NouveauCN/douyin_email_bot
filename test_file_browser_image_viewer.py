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

    def test_comics_gallery_scans_nested_images_in_order_without_delete(self):
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
        comics_card = page[page.index('class="card comics-card"'):]
        self.assertNotIn('class="del-btn"', comics_card.split('</div>\n  </div>', 1)[0])

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
