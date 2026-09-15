"""Focused tests for the QR login web API."""

import sys
import sqlite3
from pathlib import Path

import pytest

import web_login
from settings_store import SettingsStore


SAME_ORIGIN_HEADERS = {"Origin": "http://localhost"}


def _clear_rate_limits():
    with web_login._rate_limit_lock:
        web_login._rate_limit_buckets.clear()


@pytest.fixture(autouse=True)
def configured_password(monkeypatch):
    monkeypatch.setenv("WEB_LOGIN_PASSWORD", "test-password")
    _clear_rate_limits()


def _unlock(client, headers=SAME_ORIGIN_HEADERS):
    return client.post("/api/unlock", json={"password": "test-password"}, headers=headers)


def test_status_only_detects_cookie_but_does_not_save_it(monkeypatch, tmp_path):
    cookie = "sessionid=secret-value; passport_csrf_token=another-secret"
    result = {
        "status": "logged_in",
        "cookie_str": cookie,
        "auth_count": 2,
        "message": "检测到登录态",
    }
    saved = {}

    class FakeSettings:
        def apply(self, changes):
            saved["changes"] = changes

    monkeypatch.setattr(web_login, "_get_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(web_login, "check_auth_cookies", lambda _: result.copy())
    monkeypatch.setattr(web_login, "_assess_quality", lambda _: ("A", True))
    monkeypatch.setattr(web_login, "_settings", FakeSettings())

    client = web_login.app.test_client()
    assert _unlock(client).status_code == 200
    response = client.get("/api/status", headers=SAME_ORIGIN_HEADERS)

    assert saved == {}
    assert response.get_json() == {
        "status": "logged_in",
        "auth_count": 2,
        "message": "已检测到登录状态（A），请填写作品链接验证保存",
    }
    assert "cookie_str" not in response.get_json()
    assert cookie not in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"


def test_status_returns_only_whitelisted_fields_and_is_not_cacheable(monkeypatch, tmp_path):
    monkeypatch.setattr(web_login, "_get_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(
        web_login,
        "check_auth_cookies",
        lambda _: {
            "status": "pending",
            "cookie_str": None,
            "auth_count": 1,
            "message": "等待扫码...",
            "unexpected": "must not be exposed",
        },
    )

    client = web_login.app.test_client()
    assert _unlock(client).status_code == 200
    response = client.get("/api/status", headers=SAME_ORIGIN_HEADERS)

    assert response.get_json() == {
        "status": "pending",
        "auth_count": 1,
        "message": "等待扫码...",
    }
    assert response.headers["Cache-Control"] == "no-store"


def test_status_does_not_touch_settings_when_cookie_is_detected(monkeypatch, tmp_path):
    cookie = "sessionid=must-not-leak"

    class FailingSettings:
        def apply(self, changes):
            raise AssertionError("status must not save")

    monkeypatch.setattr(web_login, "_get_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(
        web_login,
        "check_auth_cookies",
        lambda _: {
            "status": "logged_in",
            "cookie_str": cookie,
            "auth_count": 1,
            "message": "检测到登录态",
        },
    )
    monkeypatch.setattr(web_login, "_assess_quality", lambda _: ("A", True))
    monkeypatch.setattr(web_login, "_settings", FailingSettings())

    client = web_login.app.test_client()
    assert _unlock(client).status_code == 200
    response = client.get("/api/status", headers=SAME_ORIGIN_HEADERS)

    assert response.status_code == 200
    assert response.get_json()["status"] == "logged_in"
    assert cookie not in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"


def test_cookie_save_retries_transient_sqlite_contention(monkeypatch):
    cookie = "sessionid=retry-secret"
    calls = []

    class FlakySettings:
        def apply(self, changes):
            calls.append(changes)
            if len(calls) < 3:
                raise sqlite3.OperationalError("database is locked")

    sleeps = []
    monkeypatch.setattr(web_login, "_settings", FlakySettings())
    monkeypatch.setattr(web_login.time, "sleep", sleeps.append)

    assert web_login._persist_authenticated_cookie(cookie) is True
    assert len(calls) == 3
    assert sleeps == [0.05, 0.1]
    assert calls[-1][0]["value"] == cookie


def test_real_settings_store_rejects_cookie_only_identity_save(monkeypatch, tmp_path):
    store = SettingsStore(tmp_path / "settings.sqlite3")
    monkeypatch.setattr(web_login, "_settings", store)

    assert web_login._persist_authenticated_cookie("sessionid=without-ua") is False
    assert store.get("douyin.cookie") is None
    assert store.get("douyin.user_agent") is None
    assert store.get_revision() == 0


def test_real_settings_store_saves_cookie_and_user_agent_as_one_identity(monkeypatch, tmp_path):
    store = SettingsStore(tmp_path / "settings.sqlite3")
    monkeypatch.setattr(web_login, "_settings", store)
    user_agent = "Mozilla/5.0 (X11; Linux x86_64) Firefox/153.0"

    assert web_login._persist_authenticated_cookie("sessionid=paired", user_agent) is True
    assert store.get("douyin.cookie") == "sessionid=paired"
    assert store.get("douyin.user_agent") == user_agent
    assert store.get_revision() == 1


def test_cookie_save_permanent_failure_is_redacted(monkeypatch, caplog):
    cookie = "sessionid=must-not-leak"

    class FailingSettings:
        def apply(self, changes):
            raise sqlite3.OperationalError(f"database error involving {cookie}")

    monkeypatch.setattr(web_login, "_settings", FailingSettings())
    monkeypatch.setattr(web_login.time, "sleep", lambda _: None)

    with caplog.at_level("ERROR", logger="web_login"):
        assert web_login._persist_authenticated_cookie(cookie) is False

    assert cookie not in caplog.text
    assert "OperationalError" in caplog.text
    assert "category=sqlite_operational" in caplog.text or "category=sqlite_busy" in caplog.text


def test_cookie_save_reports_environment_override_without_secret(monkeypatch, caplog):
    cookie = "sessionid=permission-secret"

    class LockedSettings:
        def apply(self, changes):
            raise PermissionError("setting is read-only: douyin.cookie")

    monkeypatch.setattr(web_login, "_settings", LockedSettings())
    with caplog.at_level("ERROR", logger="web_login"):
        assert web_login._persist_authenticated_cookie(cookie) is False

    assert "category=environment_override" in caplog.text
    assert cookie not in caplog.text


def test_qr_response_is_not_cacheable(monkeypatch, tmp_path):
    monkeypatch.setattr(web_login, "_get_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(web_login, "screenshot_qr_code", lambda _: ("base64", "ok"))

    client = web_login.app.test_client()
    assert _unlock(client).status_code == 200
    response = client.get("/api/qr", headers=SAME_ORIGIN_HEADERS)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


def test_stop_endpoint_is_not_available():
    response = web_login.app.test_client().post("/api/stop")

    assert response.status_code == 404


def test_standalone_entrypoint_disables_flask_dotenv_loading(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "argv", ["web_login.py"])

    monkeypatch.setattr(web_login, "_get_profile_dir", lambda: Path("/tmp/test-web-login-profile"))

    def fake_run(**kwargs):
        calls.append(kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        web_login.app,
        "run",
        fake_run,
    )

    web_login.main()

    assert calls[0]["load_dotenv"] is False


def test_standalone_page_uses_remote_save_panel(monkeypatch):
    response = web_login.app.test_client().get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'id="webLoginWorkUrl"' in body
    assert "'/api'" in body
    assert "/desktop/save" in body


def test_missing_password_disables_login(monkeypatch):
    monkeypatch.delenv("WEB_LOGIN_PASSWORD")
    monkeypatch.setattr(web_login, "_password", lambda: "")
    client = web_login.app.test_client()
    assert client.post("/api/unlock", json={"password": "anything"}, headers=SAME_ORIGIN_HEADERS).status_code == 503
    assert client.get("/api/qr", headers=SAME_ORIGIN_HEADERS).status_code == 503


def test_bad_password_does_not_unlock_and_correct_password_does(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(web_login, "_get_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(web_login, "screenshot_qr_code", lambda _: called.append(True) or ("base64", "ok"))
    client = web_login.app.test_client()
    bad = client.post("/api/unlock", json={"password": "wrong"}, headers=SAME_ORIGIN_HEADERS)
    assert bad.status_code == 401
    assert client.get("/api/qr", headers=SAME_ORIGIN_HEADERS).status_code == 401
    assert _unlock(client).status_code == 200
    assert client.get("/api/qr", headers=SAME_ORIGIN_HEADERS).status_code == 200
    assert called == [True]


def test_logout_relocks_qr_api(monkeypatch, tmp_path):
    monkeypatch.setattr(web_login, "_get_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(web_login, "screenshot_qr_code", lambda _: ("base64", "ok"))
    client = web_login.app.test_client()
    assert _unlock(client).status_code == 200
    assert client.post("/api/logout", headers=SAME_ORIGIN_HEADERS).status_code == 200
    assert client.get("/api/qr", headers=SAME_ORIGIN_HEADERS).status_code == 401


def test_remote_desktop_requires_unlock_and_forwards_bounded_input(monkeypatch):
    class FakeDesktop:
        def __init__(self):
            self.events = []
            self.locked = False

        def start(self, owner):
            self.owner = owner
            return True, "远程桌面已连接"

        def active_for(self, owner):
            return False

        def frame(self, owner):
            return "data:image/jpeg;base64,ZmFrZQ==", 1280, 720

        def input(self, owner, event):
            self.events.append(event)

        def resize(self, owner, width, height):
            self.events.append({"kind": "resize", "width": width, "height": height})

        def reload(self, owner):
            self.events.append({"kind": "reload"})

        def stop(self, owner=None):
            self.locked = True
            return True

    fake = FakeDesktop()
    monkeypatch.setattr(web_login, "_remote_browser", fake)
    client = web_login.app.test_client()
    assert client.post("/api/desktop/start", headers=SAME_ORIGIN_HEADERS).status_code == 401
    assert _unlock(client).status_code == 200
    started = client.post("/api/desktop/start", json={}, headers=SAME_ORIGIN_HEADERS)
    assert started.status_code == 200
    assert started.get_json()["frame"].startswith("data:image/jpeg")
    accepted = client.post(
        "/api/desktop/input",
        json={"kind": "click", "x": 20, "y": 30, "button": "left", "click_count": 1},
        headers=SAME_ORIGIN_HEADERS,
    )
    assert accepted.status_code == 200
    assert fake.events[-1]["kind"] == "click"
    text = "  验证码  "
    accepted_text = client.post(
        "/api/desktop/input",
        json={"kind": "text", "text": text},
        headers=SAME_ORIGIN_HEADERS,
    )
    assert accepted_text.status_code == 200
    assert fake.events[-1] == {"kind": "text", "text": text}
    rejected = client.post(
        "/api/desktop/input",
        json={"kind": "click", "x": 99999, "y": 30},
        headers=SAME_ORIGIN_HEADERS,
    )
    assert rejected.status_code == 400
    assert client.post("/api/desktop/lock", headers=SAME_ORIGIN_HEADERS).status_code == 200
    assert fake.locked


def test_remote_text_input_preserves_bounded_text_and_panel_supports_submit():
    value = "  验证码  "
    assert web_login._validate_desktop_input({"kind": "text", "text": value}) == {
        "kind": "text",
        "text": value,
    }
    assert len(web_login._validate_desktop_input({"kind": "text", "text": "x" * 2048})["text"]) == 2048
    with pytest.raises(web_login.RemoteBrowserError):
        web_login._validate_desktop_input({"kind": "text", "text": "x" * 2049})

    panel = web_login.WEB_LOGIN_PANEL_HTML
    assert 'id="webLoginTextForm"' in panel
    assert 'id="webLoginTextInput"' in panel
    assert 'id="webLoginWorkUrl"' in panel
    assert '正在验证作品信息' in panel
    assert "button.disabled = true" in panel
    assert 'maxlength="2048"' in panel
    assert "kind: 'text'" in panel
    assert "focus({preventScroll: true})" in panel
    assert "setInterval(frame, 2500)" in panel


def test_remote_save_redacts_cookie_and_requires_auth(monkeypatch):
    class FakeDesktop:
        def active_for(self, owner):
            return True

        def identity(self, owner):
            return "sessionid=secret; uid=private", "Mozilla/5.0 Firefox/153.0", 2

        def start(self, owner):
            return True, "ok"

        def frame(self, owner):
            return "frame", 1280, 720

        def cookies(self, owner):
            return "sessionid=secret; uid=private", 2

        def stop(self, owner=None):
            return True

    saved = {}
    class FakeSettings:
        def apply(self, changes):
            saved["changes"] = changes

    monkeypatch.setattr(web_login, "_remote_browser", FakeDesktop())
    monkeypatch.setattr(web_login, "_settings", FakeSettings())
    monkeypatch.setattr(web_login, "_assess_quality", lambda _: ("A", True))
    monkeypatch.setattr(web_login, "validate_douyin_metadata", lambda *args, **kwargs: {"success": True})
    client = web_login.app.test_client()
    assert _unlock(client).status_code == 200
    response = client.post("/api/desktop/save", json={"url": "https://www.douyin.com/video/123"}, headers=SAME_ORIGIN_HEADERS)
    assert response.status_code == 200
    assert "secret" not in response.get_data(as_text=True)
    assert saved["changes"][0]["key"] == "douyin.cookie"


class _ActiveDesktop:
    def __init__(self, cookie="sessionid=secret; uid=private", ua="Mozilla/5.0 Firefox/153.0"):
        self.cookie, self.ua, self.active = cookie, ua, True

    def active_for(self, owner):
        return self.active

    def identity(self, owner):
        return self.cookie, self.ua, 2


@pytest.mark.parametrize("result, expected", [
    ({"success": False, "category": "access_denied", "message": "safe"}, "抖音拒绝访问"),
    ({"success": False, "category": "timeout", "message": "safe"}, "验证超时"),
    ({"success": False, "category": "unavailable", "message": "safe"}, "作品链接无效"),
    ({"success": False, "category": "token_failure", "message": "safe"}, "无法生成有效"),
])
def test_remote_save_validation_failure_keeps_existing_identity(monkeypatch, result, expected):
    desktop = _ActiveDesktop()
    monkeypatch.setattr(web_login, "_remote_browser", desktop)
    monkeypatch.setattr(web_login, "validate_douyin_metadata", lambda *args, **kwargs: result)
    saved = []

    class Settings:
        def apply_douyin_identity(self, *args):
            saved.append(args)

    monkeypatch.setattr(web_login, "_settings", Settings())
    client = web_login.app.test_client()
    assert _unlock(client).status_code == 200
    response = client.post("/api/desktop/save", json={"url": "https://www.douyin.com/video/123"}, headers=SAME_ORIGIN_HEADERS)
    assert response.status_code == 422
    assert expected in response.get_json()["message"]
    assert "safe" not in response.get_data(as_text=True)
    assert saved == []


def test_remote_save_success_passes_same_cookie_and_ua_and_does_not_download(monkeypatch):
    desktop = _ActiveDesktop()
    monkeypatch.setattr(web_login, "_remote_browser", desktop)
    calls = []
    monkeypatch.setattr(web_login, "validate_douyin_metadata", lambda *args, **kwargs: calls.append((args, kwargs)) or {"success": True})
    saved = []

    class Settings:
        def apply_douyin_identity(self, *args):
            saved.append(args)

    monkeypatch.setattr(web_login, "_settings", Settings())
    client = web_login.app.test_client()
    assert _unlock(client).status_code == 200
    response = client.post("/api/desktop/save", json={"url": "https://www.douyin.com/video/123"}, headers=SAME_ORIGIN_HEADERS)
    assert response.status_code == 200
    assert response.get_json()["message"] == "作品信息验证通过，登录状态已保存"
    assert calls[0][0] == ("https://www.douyin.com/video/123", desktop.cookie, desktop.ua)
    assert calls[0][1] == {"timeout": 15}
    assert saved == [(desktop.cookie, desktop.ua)]


def test_remote_save_requires_work_url(monkeypatch):
    monkeypatch.setattr(web_login, "_remote_browser", _ActiveDesktop())
    client = web_login.app.test_client()
    assert _unlock(client).status_code == 200
    response = client.post("/api/desktop/save", json={}, headers=SAME_ORIGIN_HEADERS)
    assert response.status_code == 400
    assert "作品链接" in response.get_json()["message"]


def test_remote_save_rejects_session_replaced_during_validation(monkeypatch):
    desktop = _ActiveDesktop()
    monkeypatch.setattr(web_login, "_remote_browser", desktop)
    def validate(*args, **kwargs):
        desktop.active = False
        return {"success": True}
    monkeypatch.setattr(web_login, "validate_douyin_metadata", validate)
    client = web_login.app.test_client()
    assert _unlock(client).status_code == 200
    response = client.post("/api/desktop/save", json={"url": "https://www.douyin.com/video/123"}, headers=SAME_ORIGIN_HEADERS)
    assert response.status_code == 409
    assert "已结束" in response.get_json()["message"]


def test_remote_save_rejects_session_expired_during_validation(monkeypatch):
    desktop = _ActiveDesktop()
    desktop.stop = lambda owner: setattr(desktop, "active", False)
    monkeypatch.setattr(web_login, "_remote_browser", desktop)
    saved = []
    monkeypatch.setattr(web_login, "_persist_authenticated_cookie", lambda *args: saved.append(args))

    def validate(*args, **kwargs):
        web_login.session["web_login_unlocked_at"] = 0
        return {"success": True}

    monkeypatch.setattr(web_login, "validate_douyin_metadata", validate)
    client = web_login.app.test_client()
    assert _unlock(client).status_code == 200
    response = client.post("/api/desktop/save", json={"url": "https://www.douyin.com/video/123"}, headers=SAME_ORIGIN_HEADERS)
    assert response.status_code == 409
    assert saved == []


@pytest.mark.parametrize("result", [True, "provider error", ["data"], {"success": "true"}])
def test_remote_validation_requires_explicit_structured_success(monkeypatch, result):
    monkeypatch.setattr(web_login, "validate_douyin_metadata", lambda *args, **kwargs: result)
    assert web_login._validate_before_save("url", "cookie", "ua")[0] is False


def test_remote_cookie_read_rejects_http_error_before_cookie_names(
    monkeypatch,
):
    class Response:
        status = 503

    class FakePage:
        def goto(self, *_args, **_kwargs):
            return Response()

    class FakeContext:
        pages = [FakePage()]

        def cookies(self):
            return [
                {"name": "sessionid", "value": "secret", "domain": ".douyin.com"},
                {"name": "uid", "value": "private", "domain": ".douyin.com"},
            ]

    browser = web_login.RemoteBrowserSession()
    browser._context = FakeContext()
    browser._owner = "owner"
    browser._call = lambda callback: callback()

    with pytest.raises(web_login.RemoteBrowserError, match="HTTP 503"):
        browser.cookies("owner")


def test_api_rejects_missing_and_cross_origin_requests(monkeypatch, tmp_path):
    monkeypatch.setattr(web_login, "_get_profile_dir", lambda: tmp_path)
    called = []
    monkeypatch.setattr(web_login, "screenshot_qr_code", lambda _: called.append(True))
    client = web_login.app.test_client()

    assert client.get("/api/qr").status_code == 403
    assert client.get(
        "/api/qr",
        headers={"Origin": "http://attacker.example", "Referer": "http://localhost/"},
    ).status_code == 403
    assert client.get("/api/qr", headers={"Referer": "http://attacker.example/"}).status_code == 403
    assert called == []


def test_allowed_origin_can_be_configured_exactly(monkeypatch, tmp_path):
    monkeypatch.setenv("WEB_LOGIN_ALLOWED_ORIGINS", "https://admin.example")
    monkeypatch.setattr(web_login, "_get_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(web_login, "screenshot_qr_code", lambda _: ("base64", "ok"))

    client = web_login.app.test_client()
    assert _unlock(client, {"Origin": "https://admin.example"}).status_code == 200
    response = client.get(
        "/api/qr", headers={"Origin": "https://admin.example"}
    )

    assert response.status_code == 200


def test_qr_rate_limit_returns_retry_after(monkeypatch, tmp_path):
    _clear_rate_limits()
    monkeypatch.setenv("WEB_LOGIN_QR_RATE_LIMIT", "1")
    monkeypatch.setenv("WEB_LOGIN_RATE_WINDOW_SECONDS", "60")
    monkeypatch.setattr(web_login, "_get_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(web_login, "screenshot_qr_code", lambda _: ("base64", "ok"))
    client = web_login.app.test_client()

    assert _unlock(client).status_code == 200
    assert client.get("/api/qr", headers=SAME_ORIGIN_HEADERS).status_code == 200
    response = client.get("/api/qr", headers=SAME_ORIGIN_HEADERS)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert response.headers["Cache-Control"] == "no-store"


def test_status_rate_limit_is_separate_from_qr(monkeypatch, tmp_path):
    _clear_rate_limits()
    monkeypatch.setenv("WEB_LOGIN_STATUS_RATE_LIMIT", "1")
    monkeypatch.setenv("WEB_LOGIN_RATE_WINDOW_SECONDS", "60")
    monkeypatch.setattr(web_login, "_get_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(
        web_login,
        "check_auth_cookies",
        lambda _: {"status": "pending", "auth_count": 0, "message": "等待扫码"},
    )
    client = web_login.app.test_client()

    assert _unlock(client).status_code == 200
    assert client.get("/api/status", headers=SAME_ORIGIN_HEADERS).status_code == 200
    response = client.get("/api/status", headers=SAME_ORIGIN_HEADERS)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"


def test_default_host_is_loopback(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["web_login.py"])
    monkeypatch.setattr(web_login, "_get_profile_dir", lambda: tmp_path)
    run_args = {}

    def fake_run(**kwargs):
        run_args.update(kwargs)

    monkeypatch.setattr(web_login.app, "run", fake_run)
    web_login.main()

    assert run_args["host"] == "127.0.0.1"
