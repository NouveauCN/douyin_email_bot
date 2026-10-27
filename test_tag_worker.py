"""Focused tests for the optional comics tag worker (no torch required).

The inference functions are mocked or exercised only through their pure
parsing helpers, so this file runs in the repository-root environment where
torch and transformers are not installed.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import file_browser
from tag_worker import inference, sidecar, worker

_FAKE_IMAGE = b"definitely-not-a-real-image"


class SidecarPathTests(unittest.TestCase):
    def test_sidecar_path_uses_full_filename_suffix(self):
        image = Path("/data/pics/20261027_134524_1000082119.jpg")
        expected = Path("/data/pics/20261027_134524_1000082119.jpg.tags.json")

        self.assertEqual(sidecar.sidecar_path_for(image), expected)
        self.assertEqual(sidecar.source_path_for(expected), image)
        self.assertTrue(sidecar.is_sidecar_filename(expected.name))
        self.assertFalse(sidecar.is_sidecar_filename(".a.jpg.tags.json"))
        self.assertFalse(sidecar.is_sidecar_filename("photo.jpg"))

    def test_atomic_write_is_utf8_indented_and_leaves_no_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a.jpg.tags.json"
            payload = {"status": "ok", "tags": ["动漫", "白衣"]}

            sidecar.atomic_write_json(target, payload)

            raw = target.read_bytes()
            self.assertTrue(raw.endswith(b"\n"))
            self.assertIn("动漫".encode("utf-8"), raw)  # ensure_ascii=False
            self.assertIn(b"\n  ", raw)  # indent=2
            self.assertEqual(json.loads(raw), payload)
            # No dot-prefixed temp file survives the atomic replace.
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()),
                             [target.name])

            sidecar.atomic_write_json(target, {"status": "failed", "attempts": 2})
            state = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(state["attempts"], 2)
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)


class SkipPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.sidecar = Path(self.tempdir.name) / "a.jpg.tags.json"

    def _write(self, payload, age_seconds=0.0):
        if isinstance(payload, (bytes, bytearray)):
            self.sidecar.write_bytes(payload)
        else:
            self.sidecar.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        if age_seconds:
            stamp = time.time() - age_seconds
            os.utime(self.sidecar, (stamp, stamp))

    def test_missing_sidecar_is_processed(self):
        self.assertTrue(sidecar.should_process(self.sidecar))

    def test_ok_sidecar_is_skipped(self):
        self._write({"status": "ok", "tags": ["动漫"]})
        self.assertFalse(sidecar.should_process(self.sidecar))

    def test_recent_failure_stays_in_cooldown(self):
        self._write({"status": "failed", "attempts": 1, "error": "x"},
                    age_seconds=60)
        self.assertFalse(sidecar.should_process(self.sidecar))

    def test_stale_failure_is_retried(self):
        self._write({"status": "failed", "attempts": 1, "error": "x"},
                    age_seconds=31 * 60)
        self.assertTrue(sidecar.should_process(self.sidecar))

    def test_failure_retry_cap_is_three_attempts(self):
        self._write({"status": "failed", "attempts": 3, "error": "x"},
                    age_seconds=2 * 60 * 60)
        self.assertFalse(sidecar.should_process(self.sidecar))

        self._write({"status": "failed", "attempts": 2, "error": "x"},
                    age_seconds=2 * 60 * 60)
        self.assertTrue(sidecar.should_process(self.sidecar))

    def test_corrupt_sidecar_is_rewritten(self):
        self._write(b"{definitely not json")
        self.assertTrue(sidecar.should_process(self.sidecar))

    def test_unknown_status_is_rewritten(self):
        self._write({"status": "weird"})
        self.assertTrue(sidecar.should_process(self.sidecar))

    def test_non_integer_attempts_counts_as_zero(self):
        self._write({"status": "failed", "attempts": "many"},
                    age_seconds=60)
        self.assertFalse(sidecar.should_process(self.sidecar))


class ModelOutputParsingTests(unittest.TestCase):
    def test_chinese_顿号_separator(self):
        text = "描述：一名盘腿而坐的动漫角色\n标签：动漫、盘腿而坐、白衣、黄背景"
        description, tags = inference.parse_model_output(text)

        self.assertEqual(description, "一名盘腿而坐的动漫角色")
        self.assertEqual(tags, ["动漫", "盘腿而坐", "白衣", "黄背景"])

    def test_comma_separators_and_extra_whitespace(self):
        text = "描述：站姿角色\n标签：  动漫,  白衣 ， 黄背景、 校园 "
        description, tags = inference.parse_model_output(text)

        self.assertEqual(description, "站姿角色")
        self.assertEqual(tags, ["动漫", "白衣", "黄背景", "校园"])

    def test_swapped_lines_and_inline_markers(self):
        description, tags = inference.parse_model_output(
            "标签：动漫、白衣 描述：单行描述"
        )
        self.assertEqual(description, "单行描述")
        self.assertEqual(tags, ["动漫", "白衣"])

        description, tags = inference.parse_model_output(
            "描述：内联描述 标签：动漫、白衣"
        )
        self.assertEqual(description, "内联描述")
        self.assertEqual(tags, ["动漫", "白衣"])

    def test_dirty_input_drops_empty_and_quoted_items(self):
        text = '描述：角色\n标签：、、"动漫"、 、白衣。'
        description, tags = inference.parse_model_output(text)

        self.assertEqual(description, "角色")
        self.assertEqual(tags, ["动漫", "白衣"])

    def test_empty_tags_line_keeps_description(self):
        description, tags = inference.parse_model_output(
            "描述：一名角色\n标签："
        )
        self.assertEqual(description, "一名角色")
        self.assertEqual(tags, [])

    def test_refusal_text_falls_back_to_description(self):
        description, tags = inference.parse_model_output(
            "我无法描述这张图片。"
        )
        self.assertEqual(description, "我无法描述这张图片。")
        self.assertEqual(tags, [])

    def test_empty_and_non_string_output(self):
        self.assertEqual(inference.parse_model_output(""), ("", []))
        self.assertEqual(inference.parse_model_output(None), ("", []))

    def test_assemble_result_cleans_and_injects_nsfw(self):
        result = inference.assemble_result(
            "描述：角色\n标签：动漫、动漫、白衣",
            {"label": "nsfw", "confidence": 0.8},
        )

        self.assertEqual(result["description"], "角色")
        self.assertEqual(result["tags"][0], "nsfw")
        self.assertEqual(result["tags"][1:], ["动漫", "白衣"])
        self.assertEqual(result["nsfw"]["label"], "nsfw")


class TagHygieneTests(unittest.TestCase):
    def test_clean_tags_deduplicates_preserving_order(self):
        self.assertEqual(
            sidecar.clean_tags(["动漫", "白衣", "动漫", "黄背景", "白衣"]),
            ["动漫", "白衣", "黄背景"],
        )

    def test_clean_tags_bounds_length_at_16(self):
        long_tag = "这是一个明显超过十六个字符上限的超长标签内容" * 2
        cleaned = sidecar.clean_tags([long_tag])

        self.assertEqual(len(cleaned), 1)
        self.assertEqual(len(cleaned[0]), 16)

    def test_clean_tags_caps_count_at_10(self):
        cleaned = sidecar.clean_tags([f"标签{i}" for i in range(15)])

        self.assertEqual(len(cleaned), 10)
        self.assertEqual(cleaned[0], "标签0")

    def test_clean_tags_drops_noise(self):
        self.assertEqual(sidecar.clean_tags(["", "   ", "、、", None, 3]), [])

    def test_nsfw_label_is_inserted_first(self):
        tags = sidecar.apply_nsfw_tag(["动漫", "白衣"], "nsfw")

        self.assertEqual(tags, ["nsfw", "动漫", "白衣"])

    def test_nsfw_label_is_deduplicated_and_capped(self):
        tags = sidecar.apply_nsfw_tag(["nsfw"] + [f"标签{i}" for i in range(12)],
                                      "NSFW")

        self.assertEqual(tags[0], "nsfw")
        self.assertEqual(tags.count("nsfw"), 1)
        self.assertEqual(len(tags), 10)

    def test_normal_label_leaves_tags_untouched(self):
        self.assertEqual(sidecar.apply_nsfw_tag(["动漫"], "normal"), ["动漫"])
        self.assertEqual(sidecar.apply_nsfw_tag([], "normal"), [])
        self.assertEqual(sidecar.apply_nsfw_tag([], "nsfw"), ["nsfw"])


class PathValidationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name) / "pics"
        self.root.mkdir()
        (self.root / ".media_locks").mkdir()
        (self.root / "ok.jpg").write_bytes(_FAKE_IMAGE)
        (self.root / "ok.jpg.tags.json").write_text("{}")
        (self.root / "note.txt").write_text("text")
        (self.root / "folder.jpg").mkdir()

    def test_valid_image_path_is_accepted(self):
        resolved = sidecar.safe_source_path(
            self.root, Path("ok.jpg"), require_image=True
        )
        self.assertEqual(resolved, self.root / "ok.jpg")

    def test_parent_escape_is_rejected(self):
        self.assertIsNone(sidecar.safe_source_path(
            self.root, Path("../evil.jpg"), require_image=True
        ))

    def test_absolute_path_outside_root_is_rejected(self):
        outside = Path(self.tempdir.name) / "outside.jpg"
        outside.write_bytes(_FAKE_IMAGE)
        self.assertIsNone(sidecar.safe_source_path(
            self.root, outside, require_image=True
        ))
        self.assertIsNone(sidecar.safe_source_path(
            self.root, Path("/etc/passwd"), require_image=True
        ))

    def test_source_root_itself_is_rejected(self):
        self.assertIsNone(sidecar.safe_source_path(self.root, self.root))
        self.assertIsNone(sidecar.safe_source_path(
            self.root, self.root, require_image=True
        ))

    def test_media_locks_directory_is_rejected(self):
        lock_file = self.root / ".media_locks" / "x.jpg"
        lock_file.write_bytes(_FAKE_IMAGE)
        self.assertIsNone(sidecar.safe_source_path(
            self.root, lock_file, require_image=True
        ))

    def test_directory_is_rejected(self):
        self.assertIsNone(sidecar.safe_source_path(
            self.root, Path("folder.jpg"), require_image=True
        ))

    def test_non_image_suffix_is_rejected(self):
        self.assertIsNone(sidecar.safe_source_path(
            self.root, Path("note.txt"), require_image=True
        ))

    def test_sidecar_requirement_rejects_images_and_directories(self):
        self.assertIsNone(sidecar.safe_source_path(
            self.root, Path("ok.jpg"), require_sidecar=True
        ))
        self.assertIsNone(sidecar.safe_source_path(
            self.root, Path("folder.jpg"), require_sidecar=True
        ))
        self.assertEqual(
            sidecar.safe_source_path(
                self.root, Path("ok.jpg.tags.json"), require_sidecar=True
            ),
            self.root / "ok.jpg.tags.json",
        )

    def test_symlink_escape_is_rejected(self):
        outside = Path(self.tempdir.name) / "outside.jpg"
        outside.write_bytes(_FAKE_IMAGE)
        link = self.root / "link.jpg"
        link.symlink_to(outside)
        self.assertIsNone(sidecar.safe_source_path(
            self.root, link, require_image=True
        ))


class OrphanCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name) / "pics"
        self.root.mkdir()
        self.locks = self.root / ".media_locks"
        self.locks.mkdir()

        # Source exists -> sidecar must survive regardless of age.
        self.keep = self.root / "keep.jpg"
        self.keep.write_bytes(_FAKE_IMAGE)
        self._sidecar(self.keep, age=2 * 60 * 60)

        # Orphans at three different ages: only the oldest is removed.
        self.gone_old = self._sidecar_named("gone_old.jpg.tags.json",
                                            age=61 * 60)
        self.gone_fresh = self._sidecar_named("gone_fresh.jpg.tags.json",
                                              age=30 * 60)
        self.gone_boundary = self._sidecar_named("gone_boundary.jpg.tags.json",
                                                 age=59 * 60)
        # Out-of-scope files that must never be touched.
        self.stray_json = self.root / "stray.json"
        self.stray_json.write_text("{}")
        self._touch(self.stray_json, 2 * 60 * 60)
        self.lonely = self.root / "lonely.jpg"
        self.lonely.write_bytes(_FAKE_IMAGE)
        self._touch(self.lonely, 2 * 60 * 60)
        self.lock_sidecar = self._sidecar_named(
            ".media_locks/x.jpg.tags.json", age=2 * 60 * 60
        )
        outside_dir = Path(self.tempdir.name) / "outside"
        outside_dir.mkdir()
        self.outside_sidecar = outside_dir / "pic.jpg.tags.json"
        self.outside_sidecar.write_text("{}")
        self._touch(self.outside_sidecar, 2 * 60 * 60)
        # Foreign *.tags.json files: not this worker's schema -> keep.
        self.foreign = self.root / "data.tags.json"
        self.foreign.write_text(
            json.dumps({"custom": "foreign"}), encoding="utf-8"
        )
        self._touch(self.foreign, 2 * 60 * 60)
        self.future = self.root / "future.jpg.tags.json"
        self.future.write_text(
            json.dumps({"version": 99, "source": "future.jpg"}),
            encoding="utf-8",
        )
        self._touch(self.future, 2 * 60 * 60)

    @staticmethod
    def _touch(path, age_seconds):
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))

    def _sidecar_named(self, relname, age):
        path = self.root / relname
        source = relname.rsplit("/", 1)[-1][:-len(sidecar.SIDECAR_SUFFIX)]
        path.write_text(
            json.dumps({"version": sidecar.SIDECAR_VERSION, "source": source,
                        "status": "ok"}),
            encoding="utf-8",
        )
        self._touch(path, age)
        return path

    def _sidecar(self, image, age):
        path = sidecar.sidecar_path_for(image)
        path.write_text(
            json.dumps({"version": sidecar.SIDECAR_VERSION,
                        "source": image.name, "status": "ok"}),
            encoding="utf-8",
        )
        self._touch(path, age)
        return path

    def test_cleanup_removes_only_stale_orphans_in_scope(self):
        removed = worker.cleanup_orphans(self.root)

        self.assertEqual(removed, 1)
        self.assertFalse(self.gone_old.exists())
        # 1-hour threshold: fresher orphans stay.
        self.assertTrue(self.gone_fresh.exists())
        self.assertTrue(self.gone_boundary.exists())
        # Source still present -> sidecar stays.
        self.assertTrue(sidecar.sidecar_path_for(self.keep).exists())
        # Scope limits: never touch images, non-sidecar JSON, dot dirs, or
        # anything outside the comics source.
        self.assertTrue(self.lonely.exists())
        self.assertTrue(self.stray_json.exists())
        self.assertTrue(self.lock_sidecar.exists())
        self.assertTrue(self.outside_sidecar.exists())
        # Ownership: foreign *.tags.json without this worker's schema stay.
        self.assertTrue(self.foreign.exists())
        self.assertTrue(self.future.exists())

    def test_cleanup_is_idempotent(self):
        self.assertEqual(worker.cleanup_orphans(self.root), 1)
        self.assertEqual(worker.cleanup_orphans(self.root), 0)


class ScanOrderTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name) / "pics"
        self.root.mkdir()
        now = time.time()

        def make(relname, age):
            path = self.root / relname
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_FAKE_IMAGE)
            stamp = now - age
            os.utime(path, (stamp, stamp))
            return path

        self.old = make("old.jpg", 300)
        self.mid = make("mid.png", 200)
        self.upper = make("upper.JPG", 250)
        self.new = make("new.gif", 100)
        self.nested = make("sub/deep.webp", 50)
        # Filtered out: wrong extension, dot files, upload temps, sidecars.
        make("skip.txt", 10)
        make(".hidden.jpg", 10)
        make(".photo.jpg.deadbeef.upload", 10)
        make(".photo.jpg.tags.json", 10)
        make("photo.jpg.tags.json", 10)

    def test_scan_returns_images_newest_first(self):
        scanned = worker.scan_images(self.root)

        self.assertEqual(
            scanned,
            [self.nested, self.new, self.mid, self.upper, self.old],
        )

    def test_scan_skips_sidecars_dotfiles_and_bad_extensions(self):
        names = [path.name for path in worker.scan_images(self.root)]

        self.assertNotIn("photo.jpg.tags.json", names)
        self.assertNotIn(".photo.jpg.tags.json", names)
        self.assertNotIn(".hidden.jpg", names)
        self.assertNotIn(".photo.jpg.deadbeef.upload", names)
        self.assertNotIn("skip.txt", names)

    def test_scan_is_empty_for_missing_root(self):
        self.assertEqual(worker.scan_images(self.root / "nope"), [])


class GalleryInvisibilityTests(unittest.TestCase):
    """Regression: *.tags.json stays invisible to the comics gallery."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.pics = Path(self.tempdir.name) / "pics"
        self.pics.mkdir()
        (self.pics / "pic.jpg").write_bytes(_FAKE_IMAGE)
        (self.pics / "pic.jpg.tags.json").write_text(
            json.dumps({"status": "ok", "tags": ["动漫"]}), encoding="utf-8"
        )
        self.comics_patch = patch.object(file_browser, "_COMICS_DIR", self.pics)
        self.cache_patch = patch.object(file_browser, "_COMICS_SCAN_CACHE", None)
        self.comics_patch.start()
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.addCleanup(self.comics_patch.stop)

    def test_collect_comics_images_returns_only_images(self):
        images = file_browser._collect_comics_images()

        self.assertEqual([image["name"] for image in images], ["pic.jpg"])
        self.assertNotIn(
            "pic.jpg.tags.json",
            [image["name"] for image in images],
        )
        self.assertNotIn(".json", file_browser._IMAGE_EXTS)

    def test_image_extension_set_matches_file_browser(self):
        # Both walks filter on this set; a drift here would expose sidecars.
        self.assertEqual(sidecar.IMAGE_EXTS, file_browser._IMAGE_EXTS)


class ProcessOneTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.addCleanup(worker._write_failures.clear)
        self.root = Path(self.tempdir.name)
        self.image = self.root / "img.jpg"
        self.image.write_bytes(_FAKE_IMAGE)
        self.sidecar_path = sidecar.sidecar_path_for(self.image)

    def _state(self):
        return json.loads(self.sidecar_path.read_text(encoding="utf-8"))

    def test_success_writes_complete_sidecar(self):
        result = {
            "nsfw": {"label": "normal", "confidence": 0.9994},
            "description": "一名盘腿而坐的动漫角色",
            "tags": ["动漫", "盘腿而坐"],
        }
        with patch.object(inference, "tag_image", return_value=result) as mock:
            status = worker.process_one(self.image, threads=4)

        self.assertEqual(status, "ok")
        mock.assert_called_once_with(_FAKE_IMAGE, threads=4)
        state = self._state()
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["source"], "img.jpg")
        self.assertEqual(state["status"], "ok")
        self.assertEqual(state["model"], inference.VLM_MODEL)
        self.assertEqual(state["nsfw"],
                         {"label": "normal", "confidence": 0.9994})
        self.assertEqual(state["description"], "一名盘腿而坐的动漫角色")
        self.assertEqual(state["tags"], ["动漫", "盘腿而坐"])
        self.assertTrue(state["generated_at"])
        self.assertEqual(
            sorted(p.name for p in self.root.iterdir()),
            ["img.jpg", "img.jpg.tags.json"],
        )

    def test_inference_error_writes_failed_sidecar_and_accumulates(self):
        with patch.object(inference, "tag_image",
                          side_effect=RuntimeError("boom")) as mock:
            self.assertEqual(worker.process_one(self.image, threads=1),
                             "failed")
            self.assertEqual(worker.process_one(self.image, threads=1),
                             "failed")

        state = self._state()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["attempts"], 2)
        self.assertIn("boom", state["error"])
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["source"], "img.jpg")
        self.assertEqual(mock.call_count, 2)
        self.assertFalse(sidecar.should_process(self.sidecar_path))

    def test_read_error_skips_without_sidecar(self):
        directory = self.root / "dir.jpg"
        directory.mkdir()

        self.assertEqual(worker.process_one(directory, threads=1),
                         "read-error")
        self.assertFalse(sidecar.sidecar_path_for(directory).exists())

    def test_failed_sidecar_attempts_continue_from_previous_state(self):
        self.sidecar_path.write_text(
            json.dumps({"status": "failed", "attempts": 2, "error": "old"}),
            encoding="utf-8",
        )
        with patch.object(inference, "tag_image",
                          side_effect=RuntimeError("boom")):
            worker.process_one(self.image, threads=1)

        self.assertEqual(self._state()["attempts"], 3)


class RunOnceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.addCleanup(worker._write_failures.clear)
        self.root = Path(self.tempdir.name)
        (self.root / "a.jpg").write_bytes(_FAKE_IMAGE)
        (self.root / "b.png").write_bytes(_FAKE_IMAGE)

    def test_round_processes_then_skips_and_counts(self):
        result = {
            "nsfw": {"label": "normal", "confidence": 0.9},
            "description": "描述",
            "tags": ["动漫"],
        }
        with patch.object(inference, "tag_image", return_value=result):
            first = worker.run_once(self.root, threads=1, item_delay=0)
            second = worker.run_once(self.root, threads=1, item_delay=0)

        self.assertEqual(first["images"], 2)
        self.assertEqual(first["processed"], 2)
        self.assertEqual(first["skipped"], 0)
        self.assertEqual(first["failed"], 0)
        self.assertEqual(first["orphans_cleaned"], 0)
        self.assertEqual(first["temps_cleaned"], 0)
        self.assertEqual(second["processed"], 0)
        self.assertEqual(second["skipped"], 2)

    def test_round_counts_failures_without_stopping(self):
        with patch.object(inference, "tag_image",
                          side_effect=RuntimeError("boom")):
            stats = worker.run_once(self.root, threads=1, item_delay=0)

        self.assertEqual(stats["failed"], 2)
        self.assertEqual(stats["processed"], 0)
        self.assertEqual(stats["skipped"], 0)


class StaleTempCleanupTests(unittest.TestCase):
    """Crash-leftover ``.<name>.tags.json.<uuid>.tmp`` recycling."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name) / "pics"
        self.root.mkdir()
        (self.root / ".media_locks").mkdir()

        self.stale = self.root / ".a.jpg.tags.json.abc123.tmp"
        self.stale.write_text("partial", encoding="utf-8")
        self._touch(self.stale, 2 * 60 * 60)
        self.fresh = self.root / ".b.jpg.tags.json.def456.tmp"
        self.fresh.write_text("partial", encoding="utf-8")
        self._touch(self.fresh, 10 * 60)
        # Not ours: wrong pattern, dot dir, or outside the source.
        self.foreign = self.root / ".photo.jpg.deadbeef.upload"
        self.foreign.write_text("x", encoding="utf-8")
        self._touch(self.foreign, 2 * 60 * 60)
        self.lock_tmp = self.root / ".media_locks" / ".x.jpg.tags.json.u.tmp"
        self.lock_tmp.write_text("x", encoding="utf-8")
        self._touch(self.lock_tmp, 2 * 60 * 60)
        outside_dir = Path(self.tempdir.name) / "outside"
        outside_dir.mkdir()
        self.outside_tmp = outside_dir / ".y.jpg.tags.json.u.tmp"
        self.outside_tmp.write_text("x", encoding="utf-8")
        self._touch(self.outside_tmp, 2 * 60 * 60)

    @staticmethod
    def _touch(path, age_seconds):
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))

    def test_only_stale_worker_temps_are_recycled(self):
        removed = worker.cleanup_stale_temps(self.root)

        self.assertEqual(removed, 1)
        self.assertFalse(self.stale.exists())
        self.assertTrue(self.fresh.exists())
        self.assertTrue(self.foreign.exists())
        self.assertTrue(self.lock_tmp.exists())
        self.assertTrue(self.outside_tmp.exists())

    def test_cleanup_is_idempotent(self):
        self.assertEqual(worker.cleanup_stale_temps(self.root), 1)
        self.assertEqual(worker.cleanup_stale_temps(self.root), 0)


class WriteFailureBackoffTests(unittest.TestCase):
    """The 3-attempt cap must hold even when sidecar writes fail."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.addCleanup(worker._write_failures.clear)
        worker._write_failures.clear()
        self.root = Path(self.tempdir.name)
        self.image = self.root / "img.jpg"
        self.image.write_bytes(_FAKE_IMAGE)
        self.sidecar_path = sidecar.sidecar_path_for(self.image)

    def test_attempt_cap_holds_when_sidecar_write_fails(self):
        with patch.object(inference, "tag_image",
                          side_effect=RuntimeError("boom")), \
                patch.object(sidecar, "atomic_write_json",
                             side_effect=OSError("disk full")):
            statuses = [worker.process_one(self.image, threads=1)
                        for _ in range(4)]

        self.assertEqual(statuses, ["failed", "failed", "failed", "backoff"])
        self.assertFalse(self.sidecar_path.exists())

    def test_backoff_clears_once_a_sidecar_is_readable(self):
        with patch.object(inference, "tag_image",
                          side_effect=RuntimeError("boom")), \
                patch.object(sidecar, "atomic_write_json",
                             side_effect=OSError("disk full")):
            for _ in range(3):
                worker.process_one(self.image, threads=1)
        self.assertEqual(worker.process_one(self.image, threads=1), "backoff")

        # Disk recovered: a readable sidecar hands control back to the
        # persisted skip/cooldown policy.
        self.sidecar_path.write_text(
            json.dumps({"version": 1, "source": "img.jpg",
                        "status": "failed", "attempts": 1, "error": "old"}),
            encoding="utf-8",
        )
        result = {
            "nsfw": {"label": "normal", "confidence": 1.0},
            "description": "描述",
            "tags": ["动漫"],
        }
        with patch.object(inference, "tag_image", return_value=result):
            self.assertEqual(worker.process_one(self.image, threads=1), "ok")
        self.assertNotIn(str(self.sidecar_path), worker._write_failures)


class LazyImportTests(unittest.TestCase):
    def test_module_level_imports_stay_stdlib_only(self):
        # Importing the package in this torch-less environment already proves
        # it; assert explicitly so a future top-level import fails loudly.
        self.assertNotIn("torch", sys.modules)
        self.assertNotIn("transformers", sys.modules)
        self.assertIs(inference._torch, None)
        self.assertIs(inference._nsfw_bundle, None)
        self.assertIs(inference._vlm_bundle, None)


if __name__ == "__main__":
    unittest.main()
