"""Focused tests for the cookie extractor's public path boundaries."""

import sys
import types
from pathlib import Path

import cookie_extractor


def test_collect_douyin_cookies_allows_only_exact_bytedance_mstoken():
    cookies = [
        {"name": "sessionid", "value": "sid", "domain": ".douyin.com"},
        {"name": "sub", "value": "ok", "domain": "www.douyin.com"},
        {"name": "msToken", "value": "token", "domain": ".bytedance.com"},
        {"name": "other", "value": "no", "domain": ".bytedance.com"},
        {"name": "msToken", "value": "no", "domain": "foo.bytedance.com"},
        {"name": "msToken", "value": "no", "domain": "bytedance.com"},
    ]

    assert cookie_extractor.collect_douyin_cookies(cookies) == [
        "sessionid=sid", "sub=ok", "msToken=token"
    ]


def test_collect_douyin_cookies_discards_expired_empty_false_and_deduplicates(
    monkeypatch,
):
    monkeypatch.setattr(cookie_extractor.time, "time", lambda: 1000)
    cookies = [
        {"name": "sid", "value": "first", "domain": ".douyin.com"},
        {"name": "sid", "value": "second", "domain": ".douyin.com"},
        {"name": "msToken", "value": "expired", "domain": ".bytedance.com", "expires": 999},
        {"name": "empty", "value": " ", "domain": ".douyin.com"},
        {"name": "false", "value": "false", "domain": ".douyin.com"},
        {"name": "empty", "value": "", "domain": ".douyin.com"},
        {"name": "msToken", "value": "false", "domain": "bytedance.com"},
        {"name": "msToken", "value": "valid", "domain": ".bytedance.com", "expires": 1001},
    ]

    assert cookie_extractor.collect_douyin_cookies(cookies) == [
        "sid=first", "empty= ", "false=false", "msToken=valid"
    ]


def test_collect_douyin_cookies_prefers_douyin_token_over_cross_domain():
    cookies = [
        {"name": "msToken", "value": "cross", "domain": ".bytedance.com"},
        {"name": "msToken", "value": "douyin", "domain": ".douyin.com"},
    ]

    assert cookie_extractor.collect_douyin_cookies(cookies) == ["msToken=douyin"]


def test_collect_douyin_cookies_rejects_invalid_mstoken_on_douyin_domain():
    cookies = [
        {"name": "msToken", "value": "false", "domain": ".douyin.com"},
        {"name": "msToken", "value": "valid", "domain": ".bytedance.com"},
    ]

    assert cookie_extractor.collect_douyin_cookies(cookies) == ["msToken=valid"]


def test_collect_douyin_cookies_accepts_bare_bytedance_domain_but_not_subdomain():
    cookies = [
        {"name": "msToken", "value": "bare", "domain": "bytedance.com"},
        {"name": "other", "value": "no", "domain": "foo.bytedance.com"},
    ]

    assert cookie_extractor.collect_douyin_cookies(cookies) == ["msToken=bare"]


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
