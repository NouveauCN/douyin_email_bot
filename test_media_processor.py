"""Focused tests for conservative image and video edge-border detection."""

import tempfile
import unittest
import shutil
import subprocess
from unittest.mock import patch
from pathlib import Path

from PIL import Image, ImageDraw

from media_processor import (
    EdgeCrop,
    _video_crop_from_frames,
    detect_uniform_edges,
    process_image,
    process_video,
)


def _bordered_image(
    size=(160, 120),
    *,
    left=0,
    top=0,
    right=0,
    bottom=0,
    border=(12, 34, 56),
):
    image = Image.new("RGB", size, border)
    draw = ImageDraw.Draw(image)
    width, height = size
    box = (left, top, width - right - 1, height - bottom - 1)
    for y in range(box[1], box[3] + 1):
        color = ((y * 7) % 220 + 20, (y * 11) % 210 + 30, (y * 13) % 200 + 40)
        draw.line((box[0], y, box[2], y), fill=color)
    # Make every content row and column visibly non-uniform.
    draw.line((box[0], box[1], box[2], box[3]), fill=(250, 230, 80))
    draw.line((box[2], box[1], box[0], box[3]), fill=(80, 220, 250))
    return image


class UniformEdgeDetectionTests(unittest.TestCase):
    def test_detects_colored_borders_on_all_sides(self):
        image = _bordered_image(left=8, top=12, right=10, bottom=6)
        self.assertEqual(detect_uniform_edges(image), EdgeCrop(8, 12, 10, 6))

    def test_dark_textured_photo_is_not_treated_as_black_border(self):
        image = Image.new("RGB", (160, 120))
        pixels = image.load()
        for y in range(image.height):
            for x in range(image.width):
                # A low-key image whose values remain near black but vary at every edge.
                value = (x * 3 + y * 5) % 18
                pixels[x, y] = (value, value // 2, value // 3)
        draw = ImageDraw.Draw(image)
        draw.ellipse((35, 5, 145, 118), fill=(90, 55, 35))
        self.assertEqual(detect_uniform_edges(image), EdgeCrop())

    def test_internal_solid_lines_are_never_removed(self):
        image = _bordered_image()
        ImageDraw.Draw(image).rectangle((0, 50, 159, 55), fill=(0, 0, 0))
        self.assertEqual(detect_uniform_edges(image), EdgeCrop())

    def test_rejects_flat_region_larger_than_safety_limit(self):
        image = _bordered_image(top=40)
        self.assertEqual(detect_uniform_edges(image).top, 0)

    def test_small_webp_compression_noise_is_tolerated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bordered.webp"
            _bordered_image(top=12, bottom=8, border=(0, 0, 0)).save(
                path, "WEBP", quality=90
            )
            with Image.open(path) as image:
                crop = detect_uniform_edges(image)
        # Lossy compression can contaminate the last few border rows. Cropping
        # less is deliberately preferred over taking a content pixel.
        self.assertGreaterEqual(crop.top, 6)
        self.assertGreaterEqual(crop.bottom, 4)


class ImageProcessingTests(unittest.TestCase):
    def test_process_image_crops_atomically_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bordered.png"
            _bordered_image(left=8, top=12, right=10, bottom=6).save(path)

            result = process_image(path)

            self.assertTrue(result.changed)
            self.assertEqual(result.output_size, (142, 102))
            self.assertTrue(path.exists())
            self.assertTrue((path.parent / "bordered_original.bak").exists())
            with Image.open(path) as output:
                self.assertEqual(output.size, (142, 102))

            repeated = process_image(path)
            self.assertFalse(repeated.changed)
            self.assertIn("backup", repeated.reason)

    def test_dry_run_never_writes_a_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bordered.png"
            _bordered_image(top=12, bottom=6).save(path)

            result = process_image(path, dry_run=True)

            self.assertTrue(result.changed)
            with Image.open(path) as unchanged:
                self.assertEqual(unchanged.size, (160, 120))
            self.assertFalse((path.parent / "bordered_original.bak").exists())

    def test_write_failure_restores_the_original(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bordered.png"
            _bordered_image(top=12, bottom=6).save(path)
            original = path.read_bytes()

            with patch.object(Image.Image, "save", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    process_image(path)

            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((path.parent / "bordered_original.bak").exists())


class VideoConsensusTests(unittest.TestCase):
    def _write_frames(self, directory: Path, count: int, bordered: int) -> list[Path]:
        paths = []
        for index in range(count):
            path = directory / f"frame-{index:03d}.png"
            if index < bordered:
                image = _bordered_image(top=12, bottom=8)
            else:
                image = _bordered_image()
            image.save(path)
            paths.append(path)
        return paths

    def test_ninety_percent_consensus_allows_video_crop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            frames = self._write_frames(Path(temp_dir), 16, 15)
            crop = _video_crop_from_frames(frames, 160, 120)
        self.assertEqual(crop.top, 12)
        self.assertEqual(crop.bottom, 8)

    def test_low_consensus_rejects_video_crop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            frames = self._write_frames(Path(temp_dir), 16, 14)
            crop = _video_crop_from_frames(frames, 160, 120)
        self.assertEqual(crop, EdgeCrop())


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required"
)
class VideoProcessingTests(unittest.TestCase):
    def test_process_video_samples_and_crops_real_frames(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bordered.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=140x100:rate=25:duration=1",
                    "-vf",
                    "pad=160:120:10:10:color=black",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(path),
                ],
                check=True,
            )

            result = process_video(path)

            self.assertTrue(result.changed)
            self.assertLess(result.output_size[0], 160)
            self.assertLess(result.output_size[1], 120)
            self.assertTrue((path.parent / "bordered_original.bak").exists())
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
