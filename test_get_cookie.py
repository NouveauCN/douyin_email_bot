"""Focused tests for CLI cookie and Firefox identity persistence."""

import sys
import types

import pytest

import get_cookie


def _install_playwright(monkeypatch, browser):
    class PlaywrightContext:
        def __enter__(self):
            return types.SimpleNamespace(
                firefox=types.SimpleNamespace(
                    launch_persistent_context=lambda **_kwargs: browser
                )
            )

        def __exit__(self, *_args):
            return None

    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: PlaywrightContext()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)


def test_interactive_login_returns_actual_page_user_agent(monkeypatch, tmp_path):
    class Page:
        def goto(self, *_args, **_kwargs):
            return None

        def evaluate(self, expression):
            assert "navigator.userAgent" in expression
            return "Firefox/153.0 actual"

    class Browser:
        pages = [Page()]

        def cookies(self):
            return [
                {"name": "sessionid", "value": "sid", "domain": ".douyin.com"},
            ]

        def close(self):
            return None

    _install_playwright(monkeypatch, Browser())
    monkeypatch.setattr("builtins.input", lambda *_args: "")

    cookie, message, user_agent = get_cookie.interactive_login(
        tmp_path / "profile", validate=False, include_user_agent=True
    )

    assert cookie == "sessionid=sid"
    assert "交互式登录成功" in message
    assert user_agent == "Firefox/153.0 actual"


@pytest.mark.parametrize("cookies", [[], [{"name": "sessionid", "value": "sid", "domain": ".douyin.com"}]])
def test_interactive_login_identity_mode_always_returns_three_values(
    monkeypatch, tmp_path, cookies
):
    class Page:
        def goto(self, *_args, **_kwargs):
            return None

        def evaluate(self, _expression):
            return "Firefox/153.0 actual"

    class Browser:
        pages = [Page()]

        def cookies(self):
            return cookies

        def close(self):
            return None

    _install_playwright(monkeypatch, Browser())
    monkeypatch.setattr("builtins.input", lambda *_args: "")
    result = get_cookie.interactive_login(
        tmp_path / "profile", validate=False, include_user_agent=True
    )

    assert len(result) == 3


def test_headless_main_saves_cookie_and_ua_together(monkeypatch, tmp_path):
    saved = []
    monkeypatch.setattr(
        get_cookie,
        "extract_cookies_with_user_agent",
        lambda **_kwargs: ("sessionid=sid", "Firefox/153.0 actual", "ok"),
    )
    monkeypatch.setattr(get_cookie, "_save_cookie", lambda *args: saved.append(args))
    monkeypatch.setattr(
        sys,
        "argv",
        ["get_cookie.py", "--headless", "--no-validate", "--profile", str(tmp_path)],
    )

    get_cookie.main()

    assert saved == [("sessionid=sid", "Firefox/153.0 actual")]


def test_save_cookie_uses_atomic_identity_store(monkeypatch):
    calls = []
    monkeypatch.setattr(
        get_cookie._settings,
        "apply_douyin_identity",
        lambda cookie, user_agent: calls.append((cookie, user_agent)),
    )
    monkeypatch.setattr(
        get_cookie._settings,
        "apply",
        lambda *_args, **_kwargs: pytest.fail("cookie-only settings write used"),
    )

    get_cookie._save_cookie("sessionid=sid", "Firefox/153.0 actual")

    assert calls == [("sessionid=sid", "Firefox/153.0 actual")]


def test_save_cookie_rejects_missing_user_agent():
    with pytest.raises(ValueError, match="User-Agent"):
        get_cookie._save_cookie("sessionid=sid", None)
