"""Focused tests for comics gallery tag sidecars, filtering, and tag UI."""

import base64
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import file_browser


_TEST_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_MISSING = object()


def _sidecar(tags=None, status="ok", nsfw_label="safe", description=""):
    """A schema-valid sidecar dict; ``tags=None`` omits the field entirely."""
    data = {
        "version": 1,
        "source": "unit-test",
        "generated_at": "2026-10-27T00:00:00Z",
        "status": status,
        "model": "test-model",
        "nsfw": {"label": nsfw_label, "confidence": 0.9},
        "description": description,
    }
    if tags is not None:
        data["tags"] = tags
    return data


class ComicsTagTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
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

    def reset_caches(self):
        file_browser._invalidate_comics_scan()
        file_browser._COMICS_SIDECAR_CACHE.clear()
        file_browser._COMICS_TAGS_SUMMARY_CACHE = None

    def write_image(self, relpath, sidecar=_MISSING):
        """Write one gallery image and, unless omitted, its sidecar."""
        path = self.comics_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_TEST_PNG)
        if sidecar is _MISSING:
            return path
        sidecar_path = self.comics_dir / f"{relpath}.tags.json"
        if sidecar is None:
            sidecar_path.unlink(missing_ok=True)
        else:
            payload = sidecar if isinstance(sidecar, str) else json.dumps(
                sidecar, ensure_ascii=False
            )
            sidecar_path.write_text(payload, encoding="utf-8")
        return path

    def relpaths(self, payload):
        return [image["relpath"] for image in payload["images"]]


class ApiComicsTagTests(ComicsTagTestCase):
    def test_api_comics_returns_tags_nsfw_and_description(self):
        self.write_image(
            "a.png", _sidecar(["动漫", "少女"], nsfw_label="nsfw",
                              description="一位少女")
        )
        self.write_image("b.png")

        payload = self.client.get("/api/comics").get_json()

        self.assertEqual(payload["total"], 2)
        tagged, plain = payload["images"]
        self.assertEqual(tagged["tags"], ["动漫", "少女"])
        self.assertIs(tagged["nsfw"], True)
        self.assertEqual(tagged["description"], "一位少女")
        self.assertEqual(plain["tags"], [])
        self.assertIs(plain["nsfw"], False)
        self.assertEqual(plain["description"], "")

    def test_api_comics_tag_filter_is_exact_case_sensitive(self):
        self.write_image("a.png", _sidecar(["Anime"]))
        self.write_image("b.png", _sidecar(["动漫"]))
        self.write_image("c.png", _sidecar(["动漫", "少年"]))

        hit = self.client.get("/api/comics?tag=%E5%8A%A8%E6%BC%AB").get_json()
        self.assertEqual(hit["total"], 2)
        self.assertEqual(self.relpaths(hit), ["b.png", "c.png"])
        self.assertFalse(hit["has_more"])

        for unknown in ("anime", "ANIME", "动", "不存在的标签", "../../etc/passwd"):
            with self.subTest(tag=unknown):
                response = self.client.get(
                    "/api/comics", query_string={"tag": unknown}
                )
                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["total"], 0)
                self.assertEqual(payload["images"], [])
                self.assertFalse(payload["has_more"])

    def test_tag_filter_pagination_uses_the_filtered_total(self):
        for index in range(5):
            self.write_image(f"{index}.png", _sidecar(["动漫"]))
        self.write_image("u.png")

        first = self.client.get(
            "/api/comics", query_string={"tag": "动漫", "offset": 0, "limit": 2}
        ).get_json()
        self.assertEqual(first["total"], 5)
        self.assertEqual(len(first["images"]), 2)
        self.assertEqual(self.relpaths(first), ["0.png", "1.png"])
        self.assertTrue(first["has_more"])

        last = self.client.get(
            "/api/comics", query_string={"tag": "动漫", "offset": 4, "limit": 2}
        ).get_json()
        self.assertEqual(last["total"], 5)
        self.assertEqual(self.relpaths(last), ["4.png"])
        self.assertFalse(last["has_more"])

        middle = self.client.get(
            "/api/comics", query_string={"tag": "动漫", "offset": 2, "limit": 2}
        ).get_json()
        self.assertEqual(self.relpaths(middle), ["2.png", "3.png"])
        self.assertTrue(middle["has_more"])

        everything = self.client.get("/api/comics").get_json()
        self.assertEqual(everything["total"], 6)

    def test_untagged_pseudo_tag_selects_images_without_usable_tags(self):
        self.write_image("tagged.png", _sidecar(["动漫"]))
        self.write_image("missing.png")
        self.write_image("failed.png", _sidecar(None, status="failed"))

        payload = self.client.get(
            "/api/comics", query_string={"tag": "__untagged__"}
        ).get_json()

        self.assertEqual(payload["total"], 2)
        self.assertEqual(self.relpaths(payload), ["failed.png", "missing.png"])

    def test_unfiltered_api_reads_sidecars_only_for_the_current_page(self):
        for name in ("0.png", "1.png", "2.png"):
            self.write_image(name, _sidecar(["动漫"]))

        original = file_browser._read_comics_sidecar
        calls = []

        def counting(relpath):
            calls.append(relpath)
            return original(relpath)

        with patch.object(
            file_browser, "_read_comics_sidecar", side_effect=counting
        ):
            self.client.get("/api/comics?offset=0&limit=1")
        self.assertEqual(len(calls), 1)

        calls.clear()
        with patch.object(
            file_browser, "_read_comics_sidecar", side_effect=counting
        ):
            self.client.get(
                "/api/comics", query_string={"tag": "动漫", "offset": 0, "limit": 1}
            )
        # The filter itself must inspect the whole list, then the page slice.
        self.assertGreaterEqual(len(calls), 3)


class ComicsTagsSummaryTests(ComicsTagTestCase):
    def test_summary_counts_are_sorted_and_report_totals(self):
        self.write_image("a.png", _sidecar(["动漫", "少女"], nsfw_label="nsfw"))
        self.write_image("b.png", _sidecar(["动漫"]))
        self.write_image("c.png", _sidecar(["少女"]))
        self.write_image("d.png")
        self.write_image("e.png", _sidecar(None, status="failed"))
        self.write_image("f.png", _sidecar(["动漫"]))
        self.write_image("g.png", _sidecar(["少女"]))
        self.write_image("h.png", _sidecar(["科幻"]))

        payload = self.client.get("/api/comics/tags").get_json()

        self.assertEqual(
            payload["tags"],
            [
                {"tag": "动漫", "count": 3},
                {"tag": "少女", "count": 3},
                {"tag": "科幻", "count": 1},
            ],
        )
        self.assertEqual(payload["untagged"], 2)
        self.assertEqual(payload["nsfw"], 1)
        self.assertEqual(payload["total"], 8)

    def test_summary_cache_serves_until_expiry_then_refreshes(self):
        self.write_image("a.png", _sidecar(["动漫"]))
        self.write_image("b.png", _sidecar(["少年"]))

        first = self.client.get("/api/comics/tags").get_json()
        self.assertEqual(
            [item["tag"] for item in first["tags"]], ["动漫", "少年"]
        )

        # Rewrite a sidecar: within the TTL the cached summary must be served.
        (self.comics_dir / "b.png.tags.json").write_text(
            json.dumps(_sidecar(["少年", "科幻"]), ensure_ascii=False),
            encoding="utf-8",
        )
        second = self.client.get("/api/comics/tags").get_json()
        self.assertEqual(
            [item["tag"] for item in second["tags"]], ["动漫", "少年"]
        )

        # Inject an expired timestamp: the next read recomputes from disk.
        cached = file_browser._COMICS_TAGS_SUMMARY_CACHE
        self.assertIsNotNone(cached)
        file_browser._COMICS_TAGS_SUMMARY_CACHE = (cached[0] - 3600.0, cached[1])
        third = self.client.get("/api/comics/tags").get_json()
        self.assertEqual(
            [item["tag"] for item in third["tags"]], ["动漫", "少年", "科幻"]
        )
        self.assertEqual(third["untagged"], 0)

    def test_summary_route_ignores_path_style_query_input(self):
        self.write_image("a.png", _sidecar(["动漫"]))

        response = self.client.get(
            "/api/comics/tags", query_string={"tag": "../x", "path": "/etc"}
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["tags"], [{"tag": "动漫", "count": 1}])


class SidecarToleranceTests(ComicsTagTestCase):
    def test_faulty_sidecars_degrade_to_untagged_without_500(self):
        dirty_nsfw = _sidecar(["动漫"])
        dirty_nsfw["nsfw"] = "nsfw"
        int_description = _sidecar(["动漫"], description=123)
        cases = {
            "missing.png": (_MISSING, []),
            "bad.png": ("{not json", []),
            "list.png": (json.dumps(["not", "a", "dict"]), []),
            "failed.png": (_sidecar(None, status="failed"), []),
            "strtags.png": (_sidecar("动漫"), []),
            "nondict-nsfw.png": (dirty_nsfw, ["动漫"]),
            "intdesc.png": (int_description, ["动漫"]),
        }
        for relpath, (sidecar, _) in cases.items():
            self.write_image(relpath, sidecar)

        payload = self.client.get("/api/comics").get_json()
        self.assertEqual(payload["total"], len(cases))
        by_name = {image["relpath"]: image for image in payload["images"]}
        for relpath, (_, expected_tags) in cases.items():
            with self.subTest(case=relpath):
                image = by_name[relpath]
                self.assertEqual(image["tags"], expected_tags)
                self.assertIs(image["nsfw"], False)
        self.assertEqual(by_name["intdesc.png"]["description"], "")

        filtered = self.client.get(
            "/api/comics", query_string={"tag": "动漫"}
        ).get_json()
        self.assertEqual(filtered["total"], 2)
        summary = self.client.get("/api/comics/tags").get_json()
        self.assertEqual(summary["total"], len(cases))
        self.assertEqual(summary["untagged"], 5)
        self.assertEqual(summary["tags"], [{"tag": "动漫", "count": 2}])

        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="comicsGrid"', page.get_data(as_text=True))

    def test_missing_comics_source_returns_empty_results(self):
        shutil.rmtree(self.comics_dir)

        self.assertEqual(self.client.get("/api/comics").get_json()["total"], 0)
        summary = self.client.get("/api/comics/tags").get_json()
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["tags"], [])

    def test_sidecar_cache_is_mtime_keyed(self):
        self.write_image("a.png", _sidecar(["动漫"]))
        self.assertEqual(file_browser._read_comics_sidecar("a.png")[0], ["动漫"])

        sidecar = self.comics_dir / "a.png.tags.json"
        original_mtime = sidecar.stat().st_mtime_ns
        sidecar.write_text(
            json.dumps(_sidecar(["科幻"]), ensure_ascii=False), encoding="utf-8"
        )
        # Same mtime, different bytes: the cached parse must still win.
        os.utime(sidecar, ns=(original_mtime, original_mtime))
        self.assertEqual(file_browser._read_comics_sidecar("a.png")[0], ["动漫"])

        # A new mtime forces a re-read.
        os.utime(
            sidecar,
            ns=(original_mtime + 10 ** 9, original_mtime + 10 ** 9),
        )
        self.assertEqual(file_browser._read_comics_sidecar("a.png")[0], ["科幻"])

    def test_external_symlink_sidecar_is_untagged_and_hides_target(self):
        target = self.root / "outside-tags.json"
        target.write_text(
            json.dumps(_sidecar(["外泄标签"], description="外部描述"),
                       ensure_ascii=False),
            encoding="utf-8",
        )
        self.write_image("a.png")
        sidecar = self.comics_dir / "a.png.tags.json"
        try:
            sidecar.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")

        response = self.client.get("/api/comics")
        body = response.get_data(as_text=True)
        image = response.get_json()["images"][0]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(image["tags"], [])
        self.assertEqual(image["description"], "")
        self.assertNotIn("外泄标签", body)
        self.assertNotIn(str(target), body)

        filtered = self.client.get(
            "/api/comics", query_string={"tag": "外泄标签"}
        ).get_json()
        self.assertEqual(filtered["total"], 0)
        summary = self.client.get("/api/comics/tags").get_json()
        self.assertEqual(summary["untagged"], 1)
        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("外泄标签", page)
        self.assertNotIn(str(target), page)


class ComicsTagUiTests(ComicsTagTestCase):
    def test_responses_never_leak_paths_or_sidecar_files(self):
        self.write_image("nested/a.png", _sidecar(["动漫"]))
        self.write_image("b.png")

        for url in ("/api/comics", "/api/comics/tags"):
            with self.subTest(url=url):
                body = self.client.get(url).get_data(as_text=True)
                self.assertNotIn(str(self.comics_dir), body)
                self.assertNotIn(str(self.root), body)
                self.assertNotIn(".tags.json", body)

        payload = self.client.get("/api/comics").get_json()
        for image in payload["images"]:
            self.assertNotIn(".tags.json", image["relpath"])
            self.assertIn(
                Path(image["relpath"]).suffix.lower(),
                file_browser._IMAGE_EXTS,
            )

        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn(".tags.json", page)
        self.assertNotIn(str(self.comics_dir), page)

    def test_index_html_contains_tag_filter_controls_and_chips(self):
        self.write_image("a.png", _sidecar(["动漫"]))
        page = self.client.get("/").get_data(as_text=True)

        for needle in (
            'id="comicsTagBar"',
            'id="comicsTagFilter"',
            "全部标签（",
            "🏷️ 标签筛选",
            ".tag-chip",
            ".tag-chips",
            "data-tag=",
            "applyTagFilter",
            "selectTag",
            "/api/comics/tags",
            "__untagged__",
            "未打标（",
            "function card(",
            "comicsTagStatus",
            "comicsLoadSentinel",
            ".comics-tag-filter { display: grid; grid-template-columns: minmax(0, 1fr); gap: 6px; }",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, page)

    def test_index_renders_chips_title_and_augmented_search_text(self):
        self.write_image(
            "tagged.png",
            _sidecar(["动漫", "nsfw"], nsfw_label="nsfw",
                     description="红发动漫角色"),
        )
        self.write_image("plain.png")

        page = self.client.get("/").get_data(as_text=True)

        self.assertIn('<div class="tag-chips">', page)
        self.assertIn('data-tag="动漫"', page)
        self.assertIn('class="tag-chip nsfw"', page)
        self.assertIn('title="红发动漫角色"', page)
        self.assertIn(
            'data-search="tagged.png tagged.png 动漫 nsfw 红发动漫角色"', page
        )
        self.assertIn('data-search="plain.png plain.png"', page)
        # Only the tagged card owns chips in server-rendered HTML (the inline
        # script carries a template literal but never a data-tag attribute).
        self.assertEqual(page.count("data-tag="), 2)

    def test_injected_tags_and_description_never_render_as_markup(self):
        payload = '"><script>alert(1)</script>'
        self.write_image(
            "xss.png", _sidecar([payload, "动漫"], description=payload)
        )

        api = self.client.get("/api/comics")
        data = api.get_json()
        # The JSON endpoint is data, not an HTML context: values round-trip
        # verbatim and the response is never served as text/html.
        self.assertTrue(
            api.headers["Content-Type"].startswith("application/json")
        )
        self.assertEqual(data["images"][0]["tags"][0], payload)
        self.assertEqual(data["images"][0]["description"], payload)

        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn('title=""><script>', page)
        self.assertNotIn('data-tag=""><script>', page)
        # JS-built chips assign tag text through textContent, not innerHTML.
        self.assertIn("chip.textContent = tag", page)
        # Failed filters roll back so select/grid/pagination stay aligned.
        self.assertIn("syncTagSelect", page)
        self.assertIn("筛选失败，已恢复上一个筛选", page)


if __name__ == "__main__":
    unittest.main()
