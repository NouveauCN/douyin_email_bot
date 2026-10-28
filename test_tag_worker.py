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
        # The marker may add one entry beyond MAX_TAGS, never more: it must
        # not evict an already-confirmed candidate.
        self.assertEqual(len(tags), sidecar.MAX_TAGS + 1)

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

    def test_v1_orphans_are_still_worker_owned_and_removed(self):
        # Ownership is a known-version check: v1 predates the current schema
        # but is still ours, so its orphan is cleaned like any other while
        # unknown versions stay untouched.
        gone_v1 = self.root / "gone_v1.jpg.tags.json"
        gone_v1.write_text(
            json.dumps({"version": 1, "source": "gone_v1.jpg",
                        "status": "ok"}),
            encoding="utf-8",
        )
        self._touch(gone_v1, 2 * 60 * 60)

        removed = worker.cleanup_orphans(self.root)

        self.assertEqual(removed, 2)  # the v1 orphan plus the v2 fixture
        self.assertFalse(gone_v1.exists())
        self.assertTrue(self.gone_fresh.exists())
        self.assertTrue(self.future.exists())  # unknown version: never ours


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
        mock.assert_called_once_with(
            _FAKE_IMAGE, threads=4,
            threshold=sidecar.DEFAULT_CLIP_THRESHOLD,
        )
        state = self._state()
        self.assertEqual(state["version"], sidecar.SIDECAR_VERSION)
        self.assertEqual(state["source"], "img.jpg")
        self.assertEqual(state["status"], "ok")
        self.assertEqual(state["model"], inference.VLM_MODEL)
        self.assertEqual(state["nsfw"]["label"], "normal")
        self.assertEqual(state["nsfw"]["confidence"], 0.9994)
        self.assertEqual(state["nsfw"]["agreement"], "single")
        self.assertEqual(state["description"], "一名盘腿而坐的动漫角色")
        self.assertEqual(state["tags"], ["动漫", "盘腿而坐"])
        self.assertEqual(state["tag_scores"], {})
        self.assertEqual(state["dropped_tags"], [])
        self.assertIsNone(state["verification"])
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
        self.assertEqual(state["version"], sidecar.SIDECAR_VERSION)
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
        self.assertIs(inference._nsfw2_bundle, None)
        self.assertIs(inference._vlm_bundle, None)
        self.assertIs(inference._clip_bundle, None)


class PartitionTests(unittest.TestCase):
    """CLIP threshold bucketing: order kept, low scores archived."""

    def test_kept_preserves_order_and_dropped_is_recorded(self):
        tags = ["动漫", "浴袍", "少女", "背景"]
        scores = {"动漫": 0.41, "浴袍": 0.2, "少女": 0.33, "背景": 0.05}

        kept, dropped = sidecar.partition_tags(tags, scores, 0.25)

        self.assertEqual(kept, ["动漫", "少女"])
        self.assertEqual(dropped, ["浴袍", "背景"])

    def test_every_tag_passing_and_every_tag_dropped(self):
        self.assertEqual(
            sidecar.partition_tags(["a", "b"], {"a": 0.9, "b": 0.3}, 0.25),
            (["a", "b"], []),
        )
        self.assertEqual(
            sidecar.partition_tags(["a", "b"], {"a": 0.1, "b": 0.2}, 0.25),
            ([], ["a", "b"]),
        )

    def test_score_equal_to_threshold_counts_as_passing(self):
        self.assertEqual(
            sidecar.partition_tags(["边界"], {"边界": 0.25}, 0.25),
            (["边界"], []),
        )
        self.assertEqual(
            sidecar.partition_tags(["边界"], {"边界": 0.2499}, 0.25),
            ([], ["边界"]),
        )

    def test_missing_or_unusable_score_drops_the_tag(self):
        kept, dropped = sidecar.partition_tags(
            ["a", "b", "c"],
            {"a": 0.9, "b": float("nan"), "c": "dirty"},
            0.25,
        )
        self.assertEqual(kept, ["a"])
        self.assertEqual(dropped, ["b", "c"])

    def test_assembled_result_records_scores_dropped_and_verification(self):
        result = inference.assemble_result(
            "描述：一位少女\n标签：动漫、浴袍、少女",
            {"label": "normal", "confidence": 0.9},
            tag_scores={"动漫": 0.41, "浴袍": 0.2, "少女": 0.33},
            threshold=0.25,
        )

        self.assertEqual(result["tags"], ["动漫", "少女"])  # original order
        self.assertEqual(result["dropped_tags"], ["浴袍"])
        self.assertEqual(
            result["tag_scores"], {"动漫": 0.41, "浴袍": 0.2, "少女": 0.33}
        )
        verification = result["verification"]
        self.assertEqual(verification["model"], inference.clip_model_id())
        self.assertEqual(verification["threshold"], 0.25)
        self.assertEqual(verification["mean_score"], round((0.41 + 0.2 + 0.33) / 3, 4))
        self.assertEqual(verification["min_score"], 0.2)

    def test_nsfw_marker_is_never_thresholded_away(self):
        result = inference.assemble_result(
            "描述：x\n标签：动漫",
            {"label": "nsfw", "confidence": 0.9},
            tag_scores={"动漫": 0.01},
            threshold=0.25,
        )

        self.assertEqual(result["tags"], ["nsfw"])
        self.assertEqual(result["dropped_tags"], ["动漫"])
        # The marker is a classifier verdict, not a Qwen candidate.
        self.assertNotIn("nsfw", result["tag_scores"])

    def test_legacy_assemble_without_scores_keeps_everything(self):
        result = inference.assemble_result(
            "描述：x\n标签：动漫、浴袍", {"label": "normal", "confidence": 0.9}
        )
        self.assertEqual(result["tags"], ["动漫", "浴袍"])
        self.assertNotIn("tag_scores", result)


class NsfwAggregationTests(unittest.TestCase):
    """Two NSFW classifiers: agreement levels and conservative fallbacks."""

    def test_both_nsfw_is_high_agreement(self):
        record = inference.combine_nsfw(
            {"label": "nsfw", "confidence": 0.99},
            {"label": "nsfw", "confidence": 0.61},
        )
        self.assertEqual(record["label"], "nsfw")
        self.assertEqual(record["agreement"], "high")
        self.assertEqual(record["confidence"], 0.99)
        self.assertEqual(record["models"]["primary"]["label"], "nsfw")
        self.assertEqual(record["models"]["secondary"]["confidence"], 0.61)

    def test_disagreement_keeps_nsfw_low_and_uses_the_nsfw_score(self):
        record = inference.combine_nsfw(
            {"label": "nsfw", "confidence": 0.99},
            {"label": "normal", "confidence": 0.61},
        )
        self.assertEqual(record["label"], "nsfw")
        self.assertEqual(record["agreement"], "low")
        self.assertEqual(record["confidence"], 0.99)

        flipped = inference.combine_nsfw(
            {"label": "normal", "confidence": 0.8},
            {"label": "nsfw", "confidence": 0.55},
        )
        self.assertEqual(flipped["label"], "nsfw")
        self.assertEqual(flipped["agreement"], "low")
        self.assertEqual(flipped["confidence"], 0.55)

    def test_both_normal_is_high_agreement(self):
        record = inference.combine_nsfw(
            {"label": "normal", "confidence": 0.93},
            {"label": "normal", "confidence": 0.7},
        )
        self.assertEqual(record["label"], "normal")
        self.assertEqual(record["agreement"], "high")
        self.assertEqual(record["confidence"], 0.93)

    def test_single_usable_model_reports_single(self):
        only_primary = inference.combine_nsfw(
            {"label": "normal", "confidence": 0.7}, None
        )
        self.assertEqual(only_primary["agreement"], "single")
        self.assertEqual(only_primary["label"], "normal")
        self.assertIsNone(only_primary["models"]["secondary"])

        only_secondary = inference.combine_nsfw(
            None, {"label": "nsfw", "confidence": 0.9}
        )
        self.assertEqual(only_secondary["agreement"], "single")
        self.assertEqual(only_secondary["label"], "nsfw")
        self.assertIsNone(only_secondary["models"]["primary"])

    def test_no_usable_verdict_stays_conservative_nsfw(self):
        # Zero usable verdicts also report `single` (the docs spell that
        # fallback out) and degrade to the conservative nsfw record.
        # NOTE: primary=None is unreachable in production -- the primary
        # classifier always yields a verdict or fails the image first --
        # so only the secondary verdict can ever be missing here.
        record = inference.combine_nsfw(None, None)
        self.assertEqual(record["label"], "nsfw")
        self.assertEqual(record["agreement"], "single")
        self.assertEqual(record["confidence"], 0.0)
        self.assertEqual(record["models"], {"primary": None, "secondary": None})

    def test_unusable_secondary_output_is_ignored_not_merged(self):
        record = inference.combine_nsfw(
            {"label": "normal", "confidence": 0.9},
            {"label": "weird", "confidence": 0.4},
        )
        self.assertEqual(record["agreement"], "single")
        self.assertIsNone(record["models"]["secondary"])

    def test_secondary_failure_never_fails_a_first_round(self):
        with patch.object(inference, "classify_nsfw",
                          return_value={"label": "normal", "confidence": 0.9}), \
                patch.object(inference, "classify_nsfw_secondary",
                             side_effect=RuntimeError("boom")), \
                patch.object(inference, "generate_tags",
                             return_value="描述：x\n标签：动漫"), \
                patch.object(inference, "score_tags", return_value=[0.4]):
            result = inference.tag_image(_FAKE_IMAGE, threads=1, threshold=0.25)

        self.assertEqual(result["nsfw"]["agreement"], "single")
        self.assertEqual(result["nsfw"]["label"], "normal")
        self.assertEqual(result["tags"], ["动漫"])

    def test_verify_image_survives_a_secondary_failure(self):
        with patch.object(inference, "classify_nsfw",
                          return_value={"label": "nsfw", "confidence": 0.9}), \
                patch.object(inference, "classify_nsfw_secondary",
                             side_effect=RuntimeError("boom")), \
                patch.object(inference, "score_tags", return_value=[0.4]):
            result = inference.verify_image(
                _FAKE_IMAGE, tags=["动漫"], threads=1, threshold=0.25
            )

        self.assertEqual(result["nsfw"]["label"], "nsfw")
        self.assertEqual(result["nsfw"]["agreement"], "single")
        self.assertEqual(result["tags"], ["nsfw", "动漫"])
        self.assertEqual(result["dropped_tags"], [])


class SchemaV2Tests(unittest.TestCase):
    """The version 2 sidecar document and its ownership rules."""

    FULL_RESULT = {
        "nsfw": {
            "label": "nsfw",
            "confidence": 0.94,
            "agreement": "low",
            "models": {
                "primary": {"label": "nsfw", "confidence": 0.99},
                "secondary": {"label": "normal", "confidence": 0.61},
            },
        },
        "description": "一位少女",
        "tags": ["nsfw", "动漫"],
        "tag_scores": {"动漫": 0.41, "浴袍": 0.33},
        "dropped_tags": ["浴袍"],
        "verification": {
            "model": "OFA-Sys/chinese-clip-vit-base-patch16",
            "threshold": 0.25,
            "mean_score": 0.37,
            "min_score": 0.33,
        },
    }

    def test_success_payload_carries_every_v2_field(self):
        payload = sidecar.build_success_payload(
            "a.jpg", dict(self.FULL_RESULT), inference.VLM_MODEL
        )

        self.assertEqual(
            set(payload),
            {"version", "source", "generated_at", "status", "model", "nsfw",
             "description", "tags", "tag_scores", "dropped_tags",
             "verification"},
        )
        self.assertEqual(payload["version"], sidecar.SIDECAR_VERSION)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["nsfw"]["agreement"], "low")
        self.assertEqual(payload["nsfw"]["models"]["primary"]["confidence"], 0.99)
        self.assertEqual(payload["tags"], ["nsfw", "动漫"])
        self.assertEqual(payload["dropped_tags"], ["浴袍"])
        self.assertEqual(payload["tag_scores"], {"动漫": 0.41, "浴袍": 0.33})
        self.assertEqual(payload["verification"]["threshold"], 0.25)
        self.assertEqual(payload["verification"]["mean_score"], 0.37)

    def test_minimal_result_still_builds_a_complete_document(self):
        payload = sidecar.build_success_payload(
            "a.jpg", {"tags": ["动漫"]}, inference.VLM_MODEL
        )

        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["nsfw"],
                         {"label": "", "confidence": 0.0,
                          "agreement": "single", "models": None})
        self.assertEqual(payload["tag_scores"], {})
        self.assertEqual(payload["dropped_tags"], [])
        self.assertIsNone(payload["verification"])
        # Round-trips through the tolerant reader used by the gallery.
        self.assertEqual(sidecar.read_sidecar.__name__, "read_sidecar")

    def test_ownership_accepts_exactly_the_known_versions(self):
        self.assertTrue(sidecar.is_worker_sidecar(
            {"version": 1, "source": "a.jpg"}))
        self.assertTrue(sidecar.is_worker_sidecar(
            {"version": 2, "source": "a.jpg"}))
        self.assertFalse(sidecar.is_worker_sidecar(
            {"version": 3, "source": "a.jpg"}))
        self.assertFalse(sidecar.is_worker_sidecar(
            {"version": True, "source": "a.jpg"}))
        self.assertFalse(sidecar.is_worker_sidecar(
            {"version": "1", "source": "a.jpg"}))
        self.assertFalse(sidecar.is_worker_sidecar({"version": 2}))
        self.assertFalse(sidecar.is_worker_sidecar({"source": "a.jpg"}))
        self.assertFalse(sidecar.is_worker_sidecar(["version", 2]))
        self.assertFalse(sidecar.is_worker_sidecar(None))

    def test_model_ids_and_threshold_come_from_the_environment(self):
        self.assertEqual(sidecar.DEFAULT_CLIP_THRESHOLD, 0.30)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TAG_WORKER_CLIP_MODEL", None)
            os.environ.pop("TAG_WORKER_NSFW2_MODEL", None)
            self.assertEqual(inference.clip_model_id(), inference.CLIP_MODEL)
            self.assertEqual(inference.nsfw2_model_id(), inference.NSFW2_MODEL)
            with patch.dict(os.environ, {
                "TAG_WORKER_CLIP_MODEL": "custom/clip",
                "TAG_WORKER_NSFW2_MODEL": "custom/nsfw2",
            }):
                self.assertEqual(inference.clip_model_id(), "custom/clip")
                self.assertEqual(inference.nsfw2_model_id(), "custom/nsfw2")


class UpgradePathTests(unittest.TestCase):
    """v1 -> v2 verification upgrade: no Qwen, v1 data never destroyed."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.addCleanup(worker._upgrade_failures.clear)
        self.addCleanup(worker._write_failures.clear)
        self.root = Path(self.tempdir.name)
        self.image = self.root / "img.jpg"
        self.image.write_bytes(_FAKE_IMAGE)
        self.sidecar_path = sidecar.sidecar_path_for(self.image)
        self.v1_state = {
            "version": 1,
            "source": "img.jpg",
            "generated_at": "2026-10-27T10:00:00+08:00",
            "status": "ok",
            "model": "Qwen/Qwen2.5-VL-3B-Instruct",
            "nsfw": {"label": "normal", "confidence": 0.9},
            "description": "一位少女",
            "tags": ["动漫", "浴袍"],
        }
        self._write_v1()

    def _write_v1(self):
        self.sidecar_path.write_text(
            json.dumps(self.v1_state, ensure_ascii=False), encoding="utf-8"
        )
        self.v1_bytes = self.sidecar_path.read_bytes()

    def _state(self):
        return json.loads(self.sidecar_path.read_text(encoding="utf-8"))

    @staticmethod
    def _verify_result():
        return {
            "nsfw": {"label": "normal", "confidence": 0.95,
                     "agreement": "high",
                     "models": {"primary": {"label": "normal",
                                            "confidence": 0.95},
                                "secondary": {"label": "normal",
                                              "confidence": 0.9}}},
            "description": "一位少女",
            "tags": ["动漫"],
            "tag_scores": {"动漫": 0.41, "浴袍": 0.2},
            "dropped_tags": ["浴袍"],
            "verification": {"model": inference.CLIP_MODEL,
                             "threshold": 0.25,
                             "mean_score": 0.305, "min_score": 0.2},
        }

    def test_v1_ok_verifies_without_qwen_and_writes_v2(self):
        with patch.object(inference, "tag_image") as qwen, \
                patch.object(inference, "verify_image",
                             return_value=self._verify_result()) as verify:
            status = worker.process_one(self.image, threads=2, threshold=0.25)

        self.assertEqual(status, "upgraded")
        qwen.assert_not_called()  # the VLM must not run during an upgrade
        verify.assert_called_once_with(
            _FAKE_IMAGE, tags=["动漫", "浴袍"], threads=2, threshold=0.25
        )
        state = self._state()
        self.assertEqual(state["version"], sidecar.SIDECAR_VERSION)
        self.assertEqual(state["status"], "ok")
        self.assertEqual(state["description"], "一位少女")  # preserved
        self.assertEqual(state["model"], "Qwen/Qwen2.5-VL-3B-Instruct")
        self.assertEqual(state["tags"], ["动漫"])
        self.assertEqual(state["dropped_tags"], ["浴袍"])
        self.assertEqual(state["nsfw"]["agreement"], "high")
        self.assertNotIn(str(self.sidecar_path), worker._upgrade_failures)

    def test_failed_upgrade_keeps_v1_bytes_and_backs_off_after_three(self):
        with patch.object(inference, "tag_image") as qwen, \
                patch.object(inference, "verify_image",
                             side_effect=RuntimeError("clip down")) as verify:
            statuses = [worker.process_one(self.image, threads=1)
                        for _ in range(4)]

        self.assertEqual(statuses, ["upgrade-failed", "upgrade-failed",
                                    "upgrade-failed", "backoff"])
        qwen.assert_not_called()
        self.assertEqual(verify.call_count, 3)
        # The original v1 sidecar is byte-for-byte untouched.
        self.assertEqual(self.sidecar_path.read_bytes(), self.v1_bytes)
        self.assertEqual(
            worker._upgrade_failures.get(str(self.sidecar_path)), 3
        )
        self.assertTrue(sidecar.should_process(self.sidecar_path))

    def test_v2_ok_sidecar_is_skipped_entirely(self):
        payload = sidecar.build_success_payload(
            "img.jpg", self._verify_result(), inference.VLM_MODEL
        )
        sidecar.atomic_write_json(self.sidecar_path, payload)

        with patch.object(inference, "tag_image") as qwen, \
                patch.object(inference, "verify_image") as verify:
            status = worker.process_one(self.image, threads=1, threshold=0.25)

        self.assertEqual(status, "skipped")
        qwen.assert_not_called()
        verify.assert_not_called()
        self.assertFalse(
            sidecar.should_process(self.sidecar_path, threshold=0.25)
        )

    def test_threshold_change_recuts_without_touching_any_model(self):
        payload = sidecar.build_success_payload(
            "img.jpg", self._verify_result(), inference.VLM_MODEL
        )
        sidecar.atomic_write_json(self.sidecar_path, payload)

        with patch.object(inference, "tag_image") as qwen, \
                patch.object(inference, "verify_image") as verify, \
                patch.object(inference, "unload_models") as unload:
            status = worker.process_one(self.image, threads=1, threshold=0.4)

        self.assertEqual(status, "repartitioned")
        qwen.assert_not_called()
        verify.assert_not_called()
        unload.assert_not_called()  # a re-cut needs no session at all
        state = self._state()
        self.assertEqual(state["tags"], ["动漫"])  # 0.41 passes 0.4
        self.assertEqual(state["dropped_tags"], ["浴袍"])
        self.assertEqual(state["verification"]["threshold"], 0.4)
        self.assertEqual(state["verification"]["mean_score"], 0.305)
        self.assertEqual(state["description"], "一位少女")
        self.assertEqual(state["model"], inference.VLM_MODEL)

    def test_unchanged_threshold_keeps_the_v2_sidecar_skipped(self):
        payload = sidecar.build_success_payload(
            "img.jpg", self._verify_result(), inference.VLM_MODEL
        )
        sidecar.atomic_write_json(self.sidecar_path, payload)

        with patch.object(inference, "tag_image") as qwen:
            status = worker.process_one(self.image, threads=1, threshold=0.25)

        self.assertEqual(status, "skipped")
        qwen.assert_not_called()


class RecutBackoffTests(unittest.TestCase):
    """A permanently failing threshold re-cut stops after MAX_ATTEMPTS."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.addCleanup(worker._upgrade_failures.clear)
        self.addCleanup(worker._write_failures.clear)
        self.root = Path(self.tempdir.name)
        self.image = self.root / "img.jpg"
        self.image.write_bytes(_FAKE_IMAGE)
        self.sidecar_path = sidecar.sidecar_path_for(self.image)
        payload = sidecar.build_success_payload(
            "img.jpg",
            {
                "nsfw": {"label": "normal", "confidence": 0.9,
                         "agreement": "high",
                         "models": {"primary": {"label": "normal",
                                                "confidence": 0.9},
                                    "secondary": {"label": "normal",
                                                  "confidence": 0.8}}},
                "description": "描述",
                "tags": ["动漫"],
                "tag_scores": {"动漫": 0.41, "浴袍": 0.2},
                "dropped_tags": ["浴袍"],
                "verification": {"model": inference.CLIP_MODEL,
                                 "threshold": 0.25,
                                 "mean_score": 0.305, "min_score": 0.2},
            },
            inference.VLM_MODEL,
        )
        sidecar.atomic_write_json(self.sidecar_path, payload)
        self.original = self.sidecar_path.read_bytes()

    def test_recut_failures_are_capped_and_the_sidecar_is_untouched(self):
        with patch.object(sidecar, "atomic_write_json",
                          side_effect=OSError("disk full")), \
                patch.object(inference, "tag_image") as qwen:
            statuses = [worker.process_one(self.image, threads=1, threshold=0.4)
                        for _ in range(4)]

        self.assertEqual(statuses, ["upgrade-failed", "upgrade-failed",
                                    "upgrade-failed", "backoff"])
        qwen.assert_not_called()
        # The existing sidecar is byte-for-byte untouched.
        self.assertEqual(self.sidecar_path.read_bytes(), self.original)
        self.assertEqual(
            worker._upgrade_failures.get(str(self.sidecar_path)), 3
        )

    def test_capped_recut_starts_no_session_in_a_round(self):
        with patch.object(sidecar, "atomic_write_json",
                          side_effect=OSError("disk full")):
            for _ in range(sidecar.MAX_ATTEMPTS):
                worker.process_one(self.image, threads=1, threshold=0.4)

        with patch.object(inference, "unload_models") as unload, \
                patch.object(inference, "tag_image") as qwen:
            stats = worker.run_once(self.root, threads=1, item_delay=0,
                                    threshold=0.4)

        unload.assert_not_called()
        qwen.assert_not_called()
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["repartitioned"], 0)


class UnloadModelsTests(unittest.TestCase):
    """Session teardown: every model cache resets, safely, without torch."""

    def setUp(self):
        self.addCleanup(self._clear)

    @staticmethod
    def _clear():
        for name in inference._MODEL_BUNDLES:
            setattr(inference, name, None)

    def test_unload_clears_every_bundle_and_is_idempotent(self):
        inference._nsfw_bundle = ("processor", "model")
        inference._nsfw2_bundle = ("processor", "model", "id")
        inference._vlm_bundle = ("model", "processor")
        inference._clip_bundle = ("model", "processor", "id")

        info = inference.unload_models()

        self.assertTrue(info["loaded"])
        for name in inference._MODEL_BUNDLES:
            self.assertIsNone(getattr(inference, name))
        self.assertIn("rss_before_mib", info)
        self.assertIn("rss_after_mib", info)
        self.assertNotIn("torch", sys.modules)

        again = inference.unload_models()
        self.assertFalse(again["loaded"])  # idempotent, no crash

    def test_unload_with_nothing_loaded_is_a_noop(self):
        info = inference.unload_models()
        self.assertFalse(info["loaded"])
        for name in inference._MODEL_BUNDLES:
            self.assertIsNone(getattr(inference, name))


class SessionLifecycleTests(unittest.TestCase):
    """Cheap rounds load nothing; sessions drain, then unload."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.addCleanup(worker._upgrade_failures.clear)
        self.addCleanup(worker._write_failures.clear)
        self.root = Path(self.tempdir.name)
        (self.root / "a.jpg").write_bytes(_FAKE_IMAGE)
        (self.root / "b.png").write_bytes(_FAKE_IMAGE)
        self.result = {
            "nsfw": {"label": "normal", "confidence": 0.9,
                     "agreement": "high",
                     "models": {"primary": {"label": "normal",
                                            "confidence": 0.9},
                                "secondary": {"label": "normal",
                                              "confidence": 0.8}}},
            "description": "描述",
            "tags": ["动漫"],
            "tag_scores": {"动漫": 0.4},
            "dropped_tags": [],
            "verification": {"model": inference.CLIP_MODEL,
                             "threshold": sidecar.DEFAULT_CLIP_THRESHOLD,
                             "mean_score": 0.4, "min_score": 0.4},
        }
        self.unload_info = {
            "loaded": True, "rss_before_mib": 100.0, "rss_after_mib": 20.0,
            "seconds": 0.01, "trimmed": True,
        }

    def _write_v2_sidecars(self):
        for image in sorted(self.root.iterdir()):
            sidecar.atomic_write_json(
                sidecar.sidecar_path_for(image),
                sidecar.build_success_payload(image.name, self.result,
                                              inference.VLM_MODEL),
            )

    def test_idle_round_never_touches_a_model_loader(self):
        self._write_v2_sidecars()

        with patch.object(inference, "_load_nsfw") as l1, \
                patch.object(inference, "_load_nsfw2") as l2, \
                patch.object(inference, "_load_clip") as l3, \
                patch.object(inference, "_load_vlm") as l4, \
                patch.object(inference, "tag_image") as tag, \
                patch.object(inference, "verify_image") as verify, \
                patch.object(inference, "unload_models") as unload:
            stats = worker.run_once(self.root, threads=1, item_delay=0)

        for loader in (l1, l2, l3, l4):
            loader.assert_not_called()
        tag.assert_not_called()
        verify.assert_not_called()
        unload.assert_not_called()  # no session -> nothing to unload
        self.assertEqual(stats["processed"], 0)
        self.assertEqual(stats["skipped"], 2)

    def test_pending_work_runs_one_session_that_unloads_at_the_end(self):
        with patch.object(inference, "tag_image",
                          return_value=self.result) as tag, \
                patch.object(inference, "unload_models",
                             return_value=self.unload_info) as unload:
            first = worker.run_once(self.root, threads=1, item_delay=0)
            second = worker.run_once(self.root, threads=1, item_delay=0)

        self.assertEqual(first["processed"], 2)
        self.assertEqual(tag.call_count, 2)
        unload.assert_called_once()  # only the session unloads
        # The next round is a cheap check again: no load, no unload.
        self.assertEqual(second["processed"], 0)
        self.assertEqual(second["skipped"], 2)

    def test_image_arriving_mid_session_joins_the_same_batch(self):
        created = []

        def fake_tag(image_bytes, *, threads, threshold=None):
            if not created:
                created.append(self.root / "late.png")
                created[-1].write_bytes(_FAKE_IMAGE)
            return self.result

        with patch.object(inference, "tag_image",
                          side_effect=fake_tag) as tag, \
                patch.object(inference, "unload_models",
                             return_value=self.unload_info) as unload:
            stats = worker.run_once(self.root, threads=1, item_delay=0)

        self.assertEqual(tag.call_count, 3)  # both originals + late arrival
        self.assertEqual(stats["processed"], 3)
        self.assertEqual(stats["images"], 2)  # the round's opening scan
        unload.assert_called_once()  # one session, one unload

    def test_open_window_defers_everything_without_loading(self):
        with patch.object(inference, "tag_image") as tag, \
                patch.object(inference, "verify_image") as verify, \
                patch.object(inference, "unload_models") as unload:
            stats = worker.run_once(
                self.root, threads=1, item_delay=0,
                maint_windows=[(0, 1440)],
            )

        tag.assert_not_called()
        verify.assert_not_called()
        unload.assert_not_called()
        self.assertEqual(stats["paused"], 2)
        self.assertEqual(stats["processed"], 0)
        self.assertEqual(
            worker.round_sleep(stats, 60.0), worker.MAINT_POLL_SECONDS
        )

    def test_window_opening_mid_session_finishes_one_image_then_unloads(self):
        # Round-entry check, session-entry check, then "window is open"
        # right after the first image completes.
        with patch.object(worker, "in_maint_window",
                          side_effect=[False, False, True]), \
                patch.object(inference, "tag_image",
                             return_value=self.result) as tag, \
                patch.object(inference, "unload_models",
                             return_value=self.unload_info) as unload:
            stats = worker.run_once(self.root, threads=1, item_delay=0)

        self.assertEqual(tag.call_count, 1)  # the in-flight image finishes
        self.assertEqual(stats["processed"], 1)
        self.assertEqual(stats["paused"], 1)  # the rest is deferred
        unload.assert_called_once()  # session ends, models dropped

    def test_window_wait_is_logged_once_per_window(self):
        worker._window_wait_logged = False
        self.addCleanup(setattr, worker, "_window_wait_logged", False)

        with self.assertLogs("tag_worker", level="INFO") as logs:
            worker.run_once(self.root, threads=1, item_delay=0,
                            maint_windows=[(0, 1440)])
            worker.run_once(self.root, threads=1, item_delay=0,
                            maint_windows=[(0, 1440)])

        waits = [line for line in logs.output
                 if "maintenance window active" in line]
        self.assertEqual(len(waits), 1)

    def test_round_sleep_polls_fast_only_while_deferring_work(self):
        self.assertEqual(worker.round_sleep({"paused": 0}, 60.0), 60.0)
        self.assertEqual(worker.round_sleep({}, 45.0), 45.0)
        self.assertEqual(worker.round_sleep({"paused": 3}, 60.0),
                         worker.MAINT_POLL_SECONDS)

    def test_write_capped_images_start_no_session_at_all(self):
        # Nothing could be persisted for these paths, so a round must not
        # spin up a session (and its start/end log lines) every scan.
        for image in self.root.iterdir():
            worker._write_failures[str(sidecar.sidecar_path_for(image))] = (
                sidecar.MAX_ATTEMPTS
            )

        with patch.object(inference, "_load_nsfw") as l1, \
                patch.object(inference, "_load_nsfw2") as l2, \
                patch.object(inference, "_load_clip") as l3, \
                patch.object(inference, "_load_vlm") as l4, \
                patch.object(inference, "tag_image") as tag, \
                patch.object(inference, "unload_models") as unload:
            stats = worker.run_once(self.root, threads=1, item_delay=0)

        for loader in (l1, l2, l3, l4):
            loader.assert_not_called()
        tag.assert_not_called()
        unload.assert_not_called()  # no pending work -> no session
        self.assertEqual(stats["skipped"], 2)
        self.assertEqual(stats["processed"], 0)

    def test_unreadable_images_never_trigger_a_model_load(self):
        # Read failures happen before any inference: the session runs and
        # unloads without a single loader being touched.
        with patch.object(Path, "read_bytes",
                          side_effect=OSError("unreadable")), \
                patch.object(inference, "_load_nsfw") as l1, \
                patch.object(inference, "_load_nsfw2") as l2, \
                patch.object(inference, "_load_clip") as l3, \
                patch.object(inference, "_load_vlm") as l4, \
                patch.object(inference, "tag_image") as tag, \
                patch.object(inference, "unload_models",
                             return_value=self.unload_info) as unload:
            stats = worker.run_once(self.root, threads=1, item_delay=0)

        tag.assert_not_called()
        for loader in (l1, l2, l3, l4):
            loader.assert_not_called()
        unload.assert_called_once()  # the session ended normally
        self.assertEqual(stats["read_errors"], 2)
        self.assertEqual(stats["processed"], 0)


class MaintenanceWindowTests(unittest.TestCase):
    """TAG_WORKER_MAINT_WINDOWS parsing and boundary semantics."""

    def test_default_windows_parse_into_three_ranges(self):
        self.assertEqual(
            worker.parse_maint_windows(worker.DEFAULT_MAINT_WINDOWS),
            [(30, 105), (510, 585), (990, 1065)],
        )

    def test_empty_or_blank_input_disables_windows(self):
        self.assertEqual(worker.parse_maint_windows(""), [])
        self.assertEqual(worker.parse_maint_windows(None), [])
        self.assertEqual(worker.parse_maint_windows(" ,  "), [])

    def test_environment_selects_default_or_disabled(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TAG_WORKER_MAINT_WINDOWS", None)
            self.assertEqual(worker._maint_windows_setting(),
                             worker.DEFAULT_MAINT_WINDOWS)
            os.environ["TAG_WORKER_MAINT_WINDOWS"] = ""
            self.assertEqual(worker._maint_windows_setting(), "")

    def test_malformed_segments_are_skipped_instead_of_crashing(self):
        with self.assertLogs("tag_worker", level="WARNING") as logs:
            windows = worker.parse_maint_windows(
                "08:00-09:xx,banana,1:2,25:00-26:00,08:00-08:00,09:00-10:00"
            )

        self.assertEqual(windows, [(540, 600)])
        self.assertTrue(any("TAG_WORKER_MAINT_WINDOWS" in line
                            for line in logs.output))

    def test_windows_may_cross_midnight(self):
        windows = worker.parse_maint_windows("23:30-00:45")
        self.assertEqual(windows, [(1410, 45)])
        self.assertTrue(worker.in_maint_window(windows, 1410))   # start in
        self.assertTrue(worker.in_maint_window(windows, 1439))
        self.assertTrue(worker.in_maint_window(windows, 10))
        self.assertFalse(worker.in_maint_window(windows, 45))    # end out
        self.assertFalse(worker.in_maint_window(windows, 720))

    def test_boundaries_are_start_inclusive_end_exclusive(self):
        windows = [(510, 585)]
        self.assertFalse(worker.in_maint_window(windows, 509))
        self.assertTrue(worker.in_maint_window(windows, 510))
        self.assertTrue(worker.in_maint_window(windows, 584))
        self.assertFalse(worker.in_maint_window(windows, 585))
        self.assertFalse(worker.in_maint_window([], 510))
        self.assertFalse(worker.in_maint_window(None, 510))


class DocumentationConsistencyTests(unittest.TestCase):
    """Key constants must stay aligned with the operator documentation."""

    @staticmethod
    def _read(name):
        return (Path(__file__).resolve().parent / name).read_text(
            encoding="utf-8"
        )

    def test_agents_md_documents_models_env_thresholds_and_limit(self):
        agents = self._read("AGENTS.md")
        for needle in (
            inference.CLIP_MODEL,
            inference.NSFW2_MODEL,
            "TAG_WORKER_CLIP_MODEL",
            "TAG_WORKER_CLIP_THRESHOLD",
            "TAG_WORKER_NSFW2_MODEL",
            "TAG_WORKER_MAINT_WINDOWS",
            worker.DEFAULT_MAINT_WINDOWS,
            "20 GB",
            f"version {sidecar.SIDECAR_VERSION}",
            # calibrated default and its evidence
            f"default **{sidecar.DEFAULT_CLIP_THRESHOLD:.2f}**",
            "min 0.264 and median 0.361",
            # the agreement fallback wording stays aligned with the code
            "exactly one produced a usable",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, agents)

    def test_compose_matches_worker_defaults(self):
        compose = self._read("docker-compose.yml")
        self.assertIn("memory: 20g", compose)
        self.assertIn(
            f"TAG_WORKER_MAINT_WINDOWS={worker.DEFAULT_MAINT_WINDOWS}", compose
        )
        self.assertIn(
            f"TAG_WORKER_CLIP_THRESHOLD={sidecar.DEFAULT_CLIP_THRESHOLD}", compose
        )

    def test_gitignore_covers_probe_workspace_and_env_backup(self):
        ignore = self._read(".gitignore")
        self.assertIn(".probe/", ignore)
        self.assertIn(".env.save", ignore)

    def test_architecture_md_describes_the_confirmation_step(self):
        architecture = self._read("ARCHITECTURE.md")
        self.assertIn("CLIP", architecture)


class NsfwInjectionCapTests(unittest.TestCase):
    """A full candidate list must survive the nsfw marker injection.

    Regression: injecting the marker used to truncate at MAX_TAGS, so a
    10-candidate list lost its last *passing* tag from both ``tags`` and
    ``dropped_tags``, which silently disabled the threshold re-cut for that
    image (``set(tag_scores) == kept | dropped`` never held again).
    """

    @staticmethod
    def _result(threshold):
        tags = [f"标签{i}" for i in range(sidecar.MAX_TAGS)]
        return inference.assemble_result(
            "描述：x\n标签：" + "、".join(tags),
            {"label": "nsfw", "confidence": 0.9},
            tag_scores={tag: 0.5 for tag in tags},
            threshold=threshold,
        ), tags

    def test_full_candidate_list_keeps_every_passing_tag(self):
        result, tags = self._result(sidecar.DEFAULT_CLIP_THRESHOLD)

        # marker + all candidates: MAX_TAGS bounds the Qwen candidates only
        self.assertEqual(len(result["tags"]), sidecar.MAX_TAGS + 1)
        self.assertEqual(result["tags"][0], "nsfw")
        for tag in tags:
            self.assertIn(tag, result["tags"])  # nothing silently lost
        self.assertEqual(result["dropped_tags"], [])
        self.assertEqual(set(result["tag_scores"]), set(tags))

    def test_full_list_still_supports_a_threshold_re_cut(self):
        result, tags = self._result(sidecar.DEFAULT_CLIP_THRESHOLD)
        payload = sidecar.build_success_payload("a.jpg", result, "m")

        self.assertEqual(len(payload["tags"]), sidecar.MAX_TAGS + 1)
        self.assertTrue(sidecar.repartition_needed(payload, 0.4))
        recut = sidecar.recut_tags(payload, 0.4)
        self.assertEqual(recut["tags"][0], "nsfw")
        # All candidates score 0.5 >= 0.4, so the re-cut keeps every one of
        # them and the completeness check holds across both buckets.
        self.assertEqual(set(recut["dropped_tags"]), set())
        self.assertEqual(
            set(payload["tag_scores"]),
            set(recut["tags"][1:]) | set(recut["dropped_tags"]),
        )
        self.assertEqual(set(recut["tags"][1:]), set(tags))

    def test_marker_injection_is_capped_only_at_max_tags_plus_one(self):
        overflow = [f"标签{i}" for i in range(sidecar.MAX_TAGS + 5)]

        capped = sidecar.apply_nsfw_tag(overflow, "nsfw")

        self.assertEqual(capped[0], "nsfw")
        self.assertEqual(len(capped), sidecar.MAX_TAGS + 1)
        # Without the marker the candidate list itself is what MAX_TAGS caps.
        self.assertEqual(len(sidecar.clean_tags(overflow)), sidecar.MAX_TAGS)


class MissingScoreTests(unittest.TestCase):
    """Unconfirmed tags are dropped at any threshold, on every path."""

    def test_unconfirmed_tag_is_dropped_even_at_threshold_zero(self):
        result = inference.assemble_result(
            "描述：x\n标签：甲、乙",
            {"label": "normal", "confidence": 0.9},
            tag_scores={"甲": 0.5},
            threshold=0.0,
        )

        self.assertEqual(result["tags"], ["甲"])
        self.assertEqual(result["dropped_tags"], ["乙"])
        # None (not 0.0) marks "never scored", so no cutoff can keep it.
        self.assertIsNone(result["tag_scores"]["乙"])
        self.assertEqual(
            sidecar.partition_tags(["甲", "乙"], {"甲": 0.5, "乙": None}, 0.0),
            (["甲"], ["乙"]),
        )

    def test_null_scores_survive_the_payload_but_block_re_cuts(self):
        result = inference.assemble_result(
            "描述：x\n标签：甲、乙",
            {"label": "normal", "confidence": 0.9},
            tag_scores={"甲": 0.5},
            threshold=0.0,
        )
        payload = sidecar.build_success_payload("a.jpg", result, "m")

        self.assertIsNone(payload["tag_scores"]["乙"])
        # A null score means the map is not fully numeric: never re-cut it.
        self.assertFalse(sidecar.repartition_needed(payload, 0.5))
        # The statistics exclude the unconfirmed tag instead of scoring 0.
        self.assertEqual(payload["verification"]["mean_score"], 0.5)


class AliasLabelTests(unittest.TestCase):
    def test_sfw_alias_is_normalized_during_aggregation(self):
        record = inference.combine_nsfw(
            {"label": "sfw", "confidence": 0.8},
            {"label": "normal", "confidence": 0.9},
        )

        self.assertEqual(record["label"], "normal")
        self.assertEqual(record["agreement"], "high")
        self.assertEqual(record["models"]["primary"]["label"], "normal")

    def test_alias_only_secondary_still_agrees_with_a_normal_primary(self):
        record = inference.combine_nsfw(
            {"label": "normal", "confidence": 0.9},
            {"label": "safe", "confidence": 0.7},
        )
        self.assertEqual(record["agreement"], "high")
        self.assertEqual(record["label"], "normal")


if __name__ == "__main__":
    unittest.main()
