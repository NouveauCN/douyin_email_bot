"""Focused tests for the QR login web API."""

import web_login


def test_status_saves_cookie_but_redacts_it_from_response(monkeypatch, tmp_path):
    cookie = "sessionid=secret-value; passport_csrf_token=another-secret"
    result = {
        "status": "logged_in",
        "cookie_str": cookie,
        "auth_count": 2,
        "message": "检测到登录态",
    }
    saved = {}

    monkeypatch.setattr(web_login, "_get_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(web_login, "check_auth_cookies", lambda _: result.copy())
    monkeypatch.setattr(web_login, "_assess_quality", lambda _: ("A", True))
    monkeypatch.setattr(
        web_login,
        "_write_env",
        lambda path, key, value: saved.update(path=path, key=key, value=value),
    )

    response = web_login.app.test_client().get("/api/status")

    assert saved == {
        "path": str(web_login._env_path),
        "key": "DOUYIN_COOKIE",
        "value": cookie,
    }
    assert response.get_json() == {
        "status": "logged_in",
        "auth_count": 2,
        "message": "检测到登录态 — A",
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

    response = web_login.app.test_client().get("/api/status")

    assert response.get_json() == {
        "status": "pending",
        "auth_count": 1,
        "message": "等待扫码...",
    }
    assert response.headers["Cache-Control"] == "no-store"


def test_qr_response_is_not_cacheable(monkeypatch, tmp_path):
    monkeypatch.setattr(web_login, "_get_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(web_login, "screenshot_qr_code", lambda _: ("base64", "ok"))

    response = web_login.app.test_client().get("/api/qr")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


def test_stop_endpoint_is_not_available():
    response = web_login.app.test_client().post("/api/stop")

    assert response.status_code == 404
