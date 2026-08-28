"""Focused tests for the cookie extractor's public path boundaries."""

import sys
import types
from pathlib import Path

import cookie_extractor


def test_extract_cookies_normalizes_string_profile_dir(monkeypatch, tmp_path):
    seen: list[Path] = []

    def fake_extract(profile_dir, *, headless):
        seen.append(profile_dir)
        return "sessionid=abc"

    monkeypatch.setattr(cookie_extractor, "extract_with_playwright", fake_extract)

    cookie, message = cookie_extractor.extract_cookies(
        profile_dir=str(tmp_path / "profile"),
        validate=False,
    )

    assert cookie == "sessionid=abc"
    assert "Cookie 已提取" in message
    assert seen == [tmp_path / "profile"]
    assert isinstance(seen[0], Path)


def test_extract_with_playwright_normalizes_string_before_mkdir(monkeypatch, tmp_path):
    class FakePage:
        def goto(self, **_kwargs):
            return None

    class FakeBrowser:
        pages = []

        def new_page(self):
            return FakePage()

        def cookies(self):
            return [{"name": "sessionid", "value": "abc", "domain": ".douyin.com"}]

        def close(self):
            return None

    class FakeFirefox:
        def launch_persistent_context(self, **kwargs):
            assert isinstance(kwargs["user_data_dir"], str)
            return FakeBrowser()

    class FakePlaywrightContext:
        def __enter__(self):
            return types.SimpleNamespace(firefox=FakeFirefox())

        def __exit__(self, *_args):
            return None

    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: FakePlaywrightContext()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

    mkdir_paths: list[Path] = []

    def fake_mkdir(path, *args, **kwargs):
        mkdir_paths.append(path)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)

    cookie = cookie_extractor.extract_with_playwright(str(tmp_path / "profile"))

    assert cookie == "sessionid=abc"
    assert mkdir_paths == [tmp_path / "profile"]
    assert isinstance(mkdir_paths[0], Path)
