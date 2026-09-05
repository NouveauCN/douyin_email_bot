"""Focused tests for the cookie extractor's public path boundaries."""

import sys
import types
from pathlib import Path

import cookie_extractor


def test_validate_cookie_network_failure_fails_closed(monkeypatch):
    class FailingClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            raise cookie_extractor.httpx.ConnectError("offline")

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(cookie_extractor.httpx, "Client", FailingClient)

    valid, reason = cookie_extractor.validate_cookie("sessionid=abc")

    assert valid is False
    assert "网络错误" in reason


def test_check_auth_cookies_network_failure_does_not_report_logged_in(
    monkeypatch, tmp_path
):
    class FakePage:
        def goto(self, *_args, **_kwargs):
            raise RuntimeError("offline")

        def inner_text(self, _selector):
            return ""

    class FakeBrowser:
        pages = []

        def new_page(self):
            return FakePage()

        def cookies(self):
            return [
                {"name": "sessionid", "value": "abc", "domain": ".douyin.com"},
                {"name": "uid", "value": "u1", "domain": ".douyin.com"},
            ]

        def close(self):
            return None

    class FakeFirefox:
        def launch_persistent_context(self, **_kwargs):
            return FakeBrowser()

    class FakePlaywrightContext:
        def __enter__(self):
            return types.SimpleNamespace(firefox=FakeFirefox())

        def __exit__(self, *_args):
            return None

    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: FakePlaywrightContext()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

    result = cookie_extractor.check_auth_cookies(tmp_path / "profile")

    assert result["status"] == "error"
    assert result["cookie_str"] is None
    assert result["auth_count"] == 2


def test_check_auth_cookies_http_error_does_not_report_logged_in(
    monkeypatch, tmp_path
):
    class Response:
        status = 403

    class FakePage:
        def goto(self, *_args, **_kwargs):
            return Response()

        def inner_text(self, _selector):
            return ""

    class FakeBrowser:
        pages = []

        def new_page(self):
            return FakePage()

        def cookies(self):
            return [
                {"name": "sessionid", "value": "abc", "domain": ".douyin.com"},
                {"name": "uid", "value": "u1", "domain": ".douyin.com"},
            ]

        def close(self):
            return None

    class FakeFirefox:
        def launch_persistent_context(self, **_kwargs):
            return FakeBrowser()

    class FakePlaywrightContext:
        def __enter__(self):
            return types.SimpleNamespace(firefox=FakeFirefox())

        def __exit__(self, *_args):
            return None

    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: FakePlaywrightContext()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

    result = cookie_extractor.check_auth_cookies(tmp_path / "profile")

    assert result["status"] == "error"
    assert result["cookie_str"] is None
    assert result["auth_count"] == 2
    assert "HTTP 403" in result["message"]


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
