"""Focused tests for manual comics tag editing (dual-write protocol).

The gallery may write a sidecar only through ``/api/comics/tags/update``
(read-modify-write flagged ``manual: true``) and delete one through
``/api/comics/tags/reset``; both routes validate paths at the same level as
the comics delete route. No test touches a real model or network.
"""

import base64
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import file_browser
from tag_worker import normalize


_TEST_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_XSS = "<img src=x onerror=alert(1)>"  # 29 chars: inside the 32-char bound


def _v2_sidecar(tags=None, status="ok", nsfw_label="normal", description="",
                manual=False, version=2, agreement="high", mean_score=0.38,
                tag_scores=None, dropped_tags=None):
    """A schema-complete version 2 sidecar (the rich read-modify-write case)."""
    data = {
        "version": version,
        "source": "unit-test",
        "generated_at": "2026-10-27T00:00:00+08:00",
        "status": status,
        "model": "Qwen/Qwen2.5-VL-3B-Instruct",
        "nsfw": {
            "label": nsfw_label,
            "confidence": 0.91,
            "agreement": agreement,
            "models": {
                "primary": {"label": nsfw_label, "confidence": 0.91},
                "secondary": {"label": nsfw_label, "confidence": 0.85},
            },
        },
        "description": description,
        "tags": list(tags if tags is not None else ["动漫"]),
        "tag_scores": dict(tag_scores if tag_scores is not None
                           else {"动漫": 0.41, "浴袍": 0.2}),
        "dropped_tags": list(dropped_tags if dropped_tags is not None
                             else ["浴袍"]),
        "verification": {
            "model": "OFA-Sys/chinese-clip-vit-base-patch16",
            "threshold": 0.25,
            "mean_score": mean_score,
            "min_score": 0.2,
        },
    }
    if status == "failed":
        data["attempts"] = 2
        data["error"] = "RuntimeError: boom"
    if manual:
        data["manual"] = True
        data["manual_edited_at"] = "2026-10-28T09:00:00+08:00"
    return data


class ManualTagTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.download_dir = self.root / "downloads"
        self.download_dir.mkdir()
        self.comics_dir = self.root / "comics" / "pics"
        self.comics_dir.mkdir(parents=True)
        for name, value in (
            ("_DOWNLOAD_DIR", self.download_dir),
            ("_COMICS_DIR", self.comics_dir),
            ("_COMICS_THUMB_CACHE", self.root / "thumb-cache"),
        ):
            patcher = patch.object(file_browser, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.reset_caches()
        self.addCleanup(self.reset_caches)
        self.client = file_browser.app.test_client()
        # Every mutating route requires a same-origin source header.
        self.client.environ_base["HTTP_ORIGIN"] = "http://localhost"

    def reset_caches(self):
        file_browser._invalidate_comics_scan()
        file_browser._COMICS_SIDECAR_CACHE.clear()
        file_browser._COMICS_TAGS_SUMMARY_CACHE = None

    def write_image(self, relpath, sidecar=None):
        """Write one gallery image; ``sidecar`` is a dict (or None: no file)."""
        path = self.comics_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_TEST_PNG)
        if sidecar is not None:
            (self.comics_dir / f"{relpath}.tags.json").write_text(
                json.dumps(sidecar, ensure_ascii=False), encoding="utf-8"
            )
        return path

    def sidecar_of(self, relpath):
        return self.comics_dir / f"{relpath}.tags.json"

    def read_sidecar(self, relpath):
        return json.loads(
            self.sidecar_of(relpath).read_text(encoding="utf-8")
        )

    def update(self, path, tags):
        return self.client.post(
            "/api/comics/tags/update", json={"path": path, "tags": tags}
        )

    def reset(self, path):
        return self.client.post(
            "/api/comics/tags/reset", json={"path": path}
        )


class ManualTagUpdateTests(ManualTagTestCase):
    def test_update_replaces_tags_and_preserves_every_other_field(self):
        self.write_image("a.png", _v2_sidecar(
            tags=["动漫", "浴袍"], description="一位少女", nsfw_label="nsfw",
        ))
        before = self.read_sidecar("a.png")

        response = self.update("a.png", ["新标签", "少女"])

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["tags"], ["新标签", "少女"])
        self.assertIs(payload["manual"], True)
        after = self.read_sidecar("a.png")
        self.assertEqual(after["tags"], ["新标签", "少女"])
        self.assertIs(after["manual"], True)
        # Read-modify-write: every other field is carried over untouched.
        for field in ("version", "source", "generated_at", "status", "model",
                      "nsfw", "description", "tag_scores", "dropped_tags",
                      "verification"):
            with self.subTest(field=field):
                self.assertEqual(after[field], before[field])
        edited = datetime.fromisoformat(after["manual_edited_at"])
        self.assertIsNotNone(edited.tzinfo)
        # Byte conventions match the worker: UTF-8 (no \u escapes), indent 2.
        raw = self.sidecar_of("a.png").read_bytes()
        self.assertIn("新标签".encode("utf-8"), raw)
        self.assertNotIn(b"\\u", raw)
        self.assertIn(b"\n  ", raw)
        self.assertTrue(raw.endswith(b"\n"))
        # The atomic dot temp never survives.
        self.assertEqual(
            sorted(p.name for p in self.comics_dir.iterdir()),
            ["a.png", "a.png.tags.json"],
        )

    def test_update_builds_a_minimal_sidecar_when_none_exists(self):
        self.write_image("plain.png")

        response = self.update("plain.png", ["动漫", "少女"])

        self.assertEqual(response.status_code, 200)
        state = self.read_sidecar("plain.png")
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["source"], "plain.png")
        self.assertEqual(state["status"], "ok")
        self.assertEqual(state["model"], "manual")
        self.assertEqual(state["description"], "")
        self.assertEqual(state["tags"], ["动漫", "少女"])
        self.assertIs(state["manual"], True)
        self.assertIn("manual_edited_at", state)
        self.assertIn("generated_at", state)
        # No confirmation blocks: the parser tolerates their absence.
        for absent in ("nsfw", "verification", "tag_scores", "dropped_tags"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, state)

    def test_minimal_sidecar_reports_null_confirmation_in_api(self):
        self.write_image("plain.png")
        self.update("plain.png", ["动漫"])

        image = self.client.get("/api/comics").get_json()["images"][0]

        self.assertEqual(image["tags"], ["动漫"])
        self.assertIs(image["manual"], True)
        self.assertIs(image["nsfw"], False)
        self.assertIsNone(image["confidence"])   # no verification block
        self.assertIsNone(image["agreement"])    # no nsfw block

    def test_update_cleans_tags_before_writing(self):
        self.write_image("a.png", _v2_sidecar())

        response = self.update(
            "a.png", ["  动漫  ", "", "少女", "动漫", "\t", "少女 ", " "]
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["tags"], ["动漫", "少女"])
        self.assertEqual(self.read_sidecar("a.png")["tags"], ["动漫", "少女"])

    def test_update_accepts_exactly_twelve_tags(self):
        self.write_image("a.png")
        tags = [f"标签{i:02d}" for i in range(12)]

        response = self.update("a.png", tags)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.read_sidecar("a.png")["tags"], tags)

    def test_update_rejects_thirteen_tags_without_writing(self):
        self.write_image("a.png", _v2_sidecar(tags=["动漫"]))
        before = self.sidecar_of("a.png").read_bytes()
        tags = [f"标签{i:02d}" for i in range(13)]

        response = self.update("a.png", tags)

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload["success"])
        self.assertIn("12", payload["message"])
        self.assertEqual(self.sidecar_of("a.png").read_bytes(), before)

    def test_update_rejects_overlong_tag_without_writing(self):
        self.write_image("fresh.png")

        response = self.update("fresh.png", ["x" * 33])

        self.assertEqual(response.status_code, 400)
        self.assertIn("32", response.get_json()["message"])
        self.assertFalse(self.sidecar_of("fresh.png").exists())
        # The 32-character boundary itself is accepted.
        ok = self.update("fresh.png", ["x" * 32])
        self.assertEqual(ok.status_code, 200)

    def test_update_rejects_non_list_and_non_string_tags(self):
        self.write_image("a.png", _v2_sidecar())
        before = self.sidecar_of("a.png").read_bytes()

        cases = ("动漫", {"动漫": 1}, 5, None, ["ok", 123], ["ok", None])
        for tags in cases:
            with self.subTest(tags=tags):
                response = self.client.post(
                    "/api/comics/tags/update",
                    json={"path": "a.png", "tags": tags},
                )
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()["success"])
                self.assertEqual(
                    self.sidecar_of("a.png").read_bytes(), before
                )
        # A request without a tags key at all is also rejected.
        missing = self.client.post(
            "/api/comics/tags/update", json={"path": "a.png"}
        )
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(self.sidecar_of("a.png").read_bytes(), before)

    def test_update_of_failed_sidecar_becomes_visible_ok(self):
        self.write_image("a.png", _v2_sidecar(status="failed", tags=[]))

        response = self.update("a.png", ["救回的标签"])

        self.assertEqual(response.status_code, 200)
        state = self.read_sidecar("a.png")
        self.assertEqual(state["status"], "ok")
        self.assertEqual(state["attempts"], 2)      # preserved, not reset
        self.assertEqual(state["error"], "RuntimeError: boom")
        image = self.client.get("/api/comics").get_json()["images"][0]
        self.assertEqual(image["tags"], ["救回的标签"])
        self.assertIs(image["manual"], True)

    def test_errors_and_successes_never_leak_absolute_paths(self):
        self.write_image("a.png", _v2_sidecar())

        for response in (
            self.update("../escape.png", ["x"]),
            self.update("/etc/passwd", ["x"]),
            self.update("missing.png", ["x"]),
            self.update("a.png", ["y" * 40]),
            self.update("a.png", ["ok"]),
            self.reset("../escape.png"),
            self.reset("a.png"),
        ):
            with self.subTest(code=response.status_code):
                body = response.get_data(as_text=True)
                self.assertNotIn(str(self.comics_dir), body)
                self.assertNotIn(str(self.root), body)
                self.assertNotIn(".tags.json", body)

    def test_non_object_bodies_return_json_400(self):
        self.write_image("a.png", _v2_sidecar(tags=["动漫"]))
        before = self.sidecar_of("a.png").read_bytes()
        urls = ("/api/comics/tags/update", "/api/comics/tags/reset")

        for body in ([1, 2], "x", 5, True):
            for url in urls:
                with self.subTest(url=url, body=body):
                    response = self.client.post(url, json=body)
                    self.assertEqual(response.status_code, 400)
                    payload = response.get_json()  # JSON, never an HTML 500
                    self.assertFalse(payload["success"])
                    self.assertIn("message", payload)
        # A missing body entirely is the same client error, not a crash.
        for url in urls:
            with self.subTest(url=url, empty=True):
                response = self.client.post(url)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()["success"])

        self.assertEqual(self.sidecar_of("a.png").read_bytes(), before)

    def test_nul_in_path_returns_json_400_without_writing(self):
        self.write_image("a.png", _v2_sidecar(tags=["动漫"]))
        before = self.sidecar_of("a.png").read_bytes()

        for url in ("/api/comics/tags/update", "/api/comics/tags/reset"):
            with self.subTest(url=url):
                response = self.client.post(
                    url, json={"path": "a\x00.png", "tags": ["x"]}
                )
                # resolve() raises ValueError on an embedded NUL; that must
                # surface as a fixed JSON 400, not an HTML 500 page.
                self.assertEqual(response.status_code, 400)
                payload = response.get_json()
                self.assertFalse(payload["success"])
                body = response.get_data(as_text=True)
                self.assertNotIn("\x00", body)
                self.assertNotIn("a\x00.png", body)
                self.assertNotIn(str(self.comics_dir), body)

        self.assertEqual(self.sidecar_of("a.png").read_bytes(), before)

    def test_control_characters_are_stripped_from_tags(self):
        self.write_image("a.png", _v2_sidecar())

        response = self.update(
            "a.png", ["a\x00b", "\x00", "动\x01漫", "  少\x1f女  ", "正常"]
        )

        self.assertEqual(response.status_code, 200)
        expected = ["ab", "动漫", "少女", "正常"]
        self.assertEqual(response.get_json()["tags"], expected)
        self.assertEqual(self.read_sidecar("a.png")["tags"], expected)
        # No control byte may survive into the file (a NUL would serialize
        # as U+FFFD in the frontend's dataset attributes).
        raw = self.sidecar_of("a.png").read_bytes()
        for byte in (b"\x00", b"\x01", b"\x1f"):
            with self.subTest(byte=byte):
                self.assertNotIn(byte, raw)

    def test_invalidation_sees_a_same_mtime_rewrite(self):
        self.write_image("a.png", _v2_sidecar(tags=["旧标签"]))
        self.assertEqual(file_browser._read_comics_sidecar("a.png")[0],
                         ["旧标签"])
        sidecar_path = self.sidecar_of("a.png")
        stamp = sidecar_path.stat().st_mtime_ns

        # Rewrite with the identical mtime (the coarse-mount-clock case the
        # mtime-keyed parse cache would otherwise keep serving forever).
        sidecar_path.write_text(
            json.dumps(_v2_sidecar(tags=["新标签"], manual=True),
                       ensure_ascii=False),
            encoding="utf-8",
        )
        os.utime(sidecar_path, ns=(stamp, stamp))
        # Without invalidation the cached parse still wins (same key).
        self.assertEqual(file_browser._read_comics_sidecar("a.png")[0],
                         ["旧标签"])

        file_browser._invalidate_comics_scan()

        self.assertEqual(file_browser._read_comics_sidecar("a.png")[0],
                         ["新标签"])
        self.assertTrue(file_browser._read_comics_sidecar("a.png")[5])


class ManualTagPathValidationTests(ManualTagTestCase):
    """The escape matrix: every bad path is refused with no disk change."""

    def setUp(self):
        super().setUp()
        self.write_image("a.png", _v2_sidecar(tags=["动漫"]))
        (self.comics_dir / "notes.txt").write_text("plain", encoding="utf-8")
        (self.comics_dir / "sub").mkdir()
        (self.comics_dir / ".media_locks").mkdir()
        outside = self.root / "outside.png"
        outside.write_bytes(_TEST_PNG)
        try:
            (self.comics_dir / "internal.png").symlink_to(
                self.comics_dir / "a.png"
            )
            (self.comics_dir / "escape.png").symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")

    def snapshot(self):
        return {
            path.relative_to(self.comics_dir).as_posix(): (
                path.read_bytes() if path.is_file() else None
            )
            for path in sorted(self.comics_dir.rglob("*"))
        }

    def assert_rejected(self, path, expected_status):
        before = self.snapshot()
        for response in (self.update(path, ["改写"]),
                         self.reset(path)):
            with self.subTest(path=path, code=response.status_code):
                self.assertEqual(response.status_code, expected_status)
                payload = response.get_json()
                self.assertFalse(payload["success"])
                self.assertIn("message", payload)
        self.assertEqual(self.snapshot(), before)

    def test_traversal_and_absolute_paths_are_rejected(self):
        for path, status in (
            ("../escape.png", 403),
            ("sub/../../escape.png", 403),
            ("a.png/../../a.png", 403),
            ("/etc/passwd", 403),
            (str(self.comics_dir / "a.png"), 403),  # absolute, inside root
            ("../../../../../etc/shadow", 403),
        ):
            self.assert_rejected(path, status)

    def test_root_directory_and_wrong_targets_are_rejected(self):
        for path, status in (
            ("", 400),              # missing path parameter
            (".", 403),             # the comics source root itself
            ("sub", 403),           # directory
            (".media_locks", 403),  # lock directory
            (".media_locks/x.png", 403),
            ("notes.txt", 403),     # non-image suffix
            ("ghost.png", 404),     # validated path, image absent
            ("internal.png", 403),  # internal symlink entry
            ("escape.png", 403),    # symlink resolving outside the source
        ):
            self.assert_rejected(path, status)

    def test_valid_path_still_updates(self):
        response = self.update("a.png", ["通过"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.read_sidecar("a.png")["tags"], ["通过"])


class ManualTagResetTests(ManualTagTestCase):
    def test_reset_deletes_sidecar_and_keeps_the_image(self):
        image = self.write_image("a.png", _v2_sidecar(
            tags=["动漫"], manual=True,
        ))
        original = image.read_bytes()

        response = self.reset("a.png")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertFalse(self.sidecar_of("a.png").exists())
        self.assertEqual(image.read_bytes(), original)
        after = self.client.get("/api/comics").get_json()["images"][0]
        self.assertEqual(after["tags"], [])
        self.assertIs(after["manual"], False)

    def test_reset_is_idempotent_without_a_sidecar(self):
        self.write_image("a.png")

        for _ in range(2):
            response = self.reset("a.png")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["success"])

    def test_reset_requires_a_path_parameter(self):
        response = self.client.post("/api/comics/tags/reset", json={})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["success"])

    def test_reset_invalidates_the_tag_summary_cache(self):
        self.write_image("a.png", _v2_sidecar(tags=["动漫"]))
        first = self.client.get("/api/comics/tags").get_json()
        self.assertEqual(first["tags"], [{"tag": "动漫", "count": 1}])

        self.reset("a.png")

        second = self.client.get("/api/comics/tags").get_json()
        self.assertEqual(second["tags"], [])
        self.assertEqual(second["untagged"], 1)


class ManualTagApiTests(ManualTagTestCase):
    def test_api_comics_carries_the_manual_flag(self):
        self.write_image("auto.png", _v2_sidecar(tags=["动漫"]))
        self.write_image("manual.png", _v2_sidecar(tags=["动漫"], manual=True))

        images = {
            image["relpath"]: image
            for image in self.client.get("/api/comics").get_json()["images"]
        }
        self.assertIs(images["auto.png"]["manual"], False)
        self.assertIs(images["manual.png"]["manual"], True)

        self.update("auto.png", ["新标签"])
        self.reset_caches()
        images = {
            image["relpath"]: image
            for image in self.client.get("/api/comics").get_json()["images"]
        }
        self.assertIs(images["auto.png"]["manual"], True)

        self.reset("auto.png")
        self.reset_caches()
        images = {
            image["relpath"]: image
            for image in self.client.get("/api/comics").get_json()["images"]
        }
        self.assertIs(images["auto.png"]["manual"], False)

    def test_update_invalidates_the_scan_and_summary_caches(self):
        self.write_image("a.png", _v2_sidecar(tags=["动漫"]))
        summary = self.client.get("/api/comics/tags").get_json()
        self.assertEqual(summary["tags"], [{"tag": "动漫", "count": 1}])

        self.update("a.png", ["新标签"])

        # The 30s summary cache was dropped by the write, so the new tag
        # shows up immediately instead of waiting for the TTL.
        refreshed = self.client.get("/api/comics/tags").get_json()
        self.assertEqual(refreshed["tags"], [{"tag": "新标签", "count": 1}])

    def test_tag_filter_sees_manually_written_tags(self):
        self.write_image("a.png")

        self.update("a.png", ["手动筛选"])

        payload = self.client.get(
            "/api/comics", query_string={"tag": "手动筛选"}
        ).get_json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["images"][0]["relpath"], "a.png")


class ManualTagUiTests(ManualTagTestCase):
    def test_page_contains_editor_controls_and_textcontent_paths(self):
        self.write_image("a.png", _v2_sidecar(tags=["动漫"], manual=True))
        page = self.client.get("/").get_data(as_text=True)

        for needle in (
            # server-rendered entry points
            'class="edit-btn"',
            'title="编辑标签"',
            "✎",
            'data-comics-path="a.png"',
            '<span class="manual-badge">已手动</span>',
            # modal controls and copy (all created via createElement)
            "openTagEditor",
            "tag-modal-overlay",
            "tag-save-btn",
            "恢复自动标签",
            "将在后台重新生成",
            "保存",
            "取消",
            "添加",
            # API endpoints
            "/api/comics/tags/update",
            "/api/comics/tags/reset",
            # XSS-safe rendering paths: user text only through textContent
            "label.textContent = tag",
            "chip.textContent = tag",
            "badge.textContent = '已手动'",
            "title.textContent = '编辑标签 · '",
            "remove.textContent = '×'",
            # bounds mirrored client-side
            "MAX_TAG_COUNT = 12",
            "MAX_TAG_CHARS = 32",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, page)

    def test_index_card_keeps_its_search_and_pagination_structure(self):
        self.write_image("a.png", _v2_sidecar(tags=["动漫"]))
        page = self.client.get("/").get_data(as_text=True)

        self.assertIn('id="comicsGrid"', page)
        # The sentinel itself only renders past the first 120 images, but the
        # pagination hook must stay wired for the JS-built cards.
        self.assertIn("comicsLoadSentinel", page)
        self.assertIn('data-search="a.png a.png 动漫"', page)
        # The editor is wired through delegation, not inline onclick, so the
        # card structure the loader rebuilds stays identical.
        self.assertNotIn("onclick=\"openTagEditor", page)

    def test_xss_payload_round_trips_json_and_never_renders_as_markup(self):
        self.write_image("xss.png")

        response = self.update("xss.png", [_XSS, "动漫"])

        self.assertEqual(response.status_code, 200)
        # The JSON endpoint is data, not an HTML context: values round-trip
        # verbatim and the response is never served as text/html.
        self.assertTrue(
            response.headers["Content-Type"].startswith("application/json")
        )
        api = self.client.get("/api/comics").get_json()
        self.assertEqual(api["images"][0]["tags"], [_XSS, "动漫"])

        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("<img src=x onerror=alert(1)>", page)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page)
        self.assertNotIn('data-tag=""><img', page)
        self.assertNotIn('title=""><img', page)
        # The modal fills labels through textContent, never innerHTML.
        self.assertIn("label.textContent = tag", page)
        self.assertNotIn("chipsBox.innerHTML", page)


class ManualTagVocabularyTests(ManualTagTestCase):
    """Manual edits normalize against the shared closed vocabulary."""

    def test_update_normalizes_aliases_and_stamps_vocabulary_version(self):
        self.write_image("a.png")

        response = self.update("a.png", ["动漫角色", "红发角色", "机甲"])

        self.assertEqual(response.status_code, 200)
        expected = ["动漫", "角色", "红发", "机甲"]  # dedup across aliases
        self.assertEqual(response.get_json()["tags"], expected)
        state = self.read_sidecar("a.png")
        self.assertEqual(state["tags"], expected)
        self.assertEqual(state["vocabulary_version"],
                         normalize.vocab_version())
        self.assertIs(state["manual"], True)

    def test_update_keeps_out_of_vocabulary_words_verbatim(self):
        self.write_image("a.png")

        response = self.update("a.png", ["机甲", "动漫角色", "深海"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["tags"],
                         ["机甲", "动漫", "角色", "深海"])

    def test_verdict_words_pass_through_without_forced_changes(self):
        # nsfw/sfw are ordinary canonical words on this route: the manual
        # authority decides, nothing is added or removed here.
        self.write_image("a.png")

        response = self.update("a.png", ["nsfw", "动漫", "sfw"])

        self.assertEqual(response.status_code, 200)
        state = self.read_sidecar("a.png")
        self.assertEqual(state["tags"], ["nsfw", "动漫", "sfw"])
        # And a manual edit without them gains none.
        self.update("a.png", ["动漫"])
        self.assertEqual(self.read_sidecar("a.png")["tags"], ["动漫"])

    def test_twelve_tag_cap_applies_to_the_pre_normalization_input(self):
        self.write_image("a.png")
        # Exactly 12 submissions: within the bound even though alias
        # expansion yields more canonical entries than were submitted.
        aliases = ["动漫角色", "卡通人物", "男性角色", "红发角色", "肌肉男",
                   "惊讶表情", "金发男孩", "年轻男子", "橙色背景",
                   "红色服装", "黑色靴子", "自然环境"]
        self.assertEqual(len(aliases), 12)

        response = self.update("a.png", aliases)

        self.assertEqual(response.status_code, 200)
        state = self.read_sidecar("a.png")
        self.assertGreater(len(state["tags"]), 12)
        self.assertEqual(state["tags"][:5],
                         ["动漫", "角色", "卡通", "男性", "红发"])
        self.assertEqual(state["vocabulary_version"],
                         normalize.vocab_version())

    def test_vocabulary_file_is_locatable_in_both_layouts(self):
        # The checkout root and the /app image root both place the data
        # file at <root>/tag_worker/vocabulary.json beside file_browser.py;
        # tag_worker resolves the same file from its own package directory.
        expected = (Path(file_browser.__file__).resolve().parent
                    / "tag_worker" / "vocabulary.json")
        self.assertEqual(expected, file_browser._COMICS_VOCABULARY)
        self.assertTrue(expected.is_file())
        data = json.loads(expected.read_text(encoding="utf-8"))
        self.assertGreater(data["version"], 0)
        self.assertEqual(normalize.DEFAULT_VOCABULARY_PATH, expected)
        self.assertEqual(normalize.vocab_version(),
                         file_browser.tag_normalize.vocab_version(
                             file_browser._COMICS_VOCABULARY))


if __name__ == "__main__":
    unittest.main()
