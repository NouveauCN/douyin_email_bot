"""Focused tests for the QR login web API."""

import sys

import web_login


SAME_ORIGIN_HEADERS = {"Origin": "http://localhost"}


def _clear_rate_limits():
    with web_login._rate_limit_lock:
        web_login._rate_limit_buckets.clear()


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

    response = web_login.app.test_client().get("/api/status", headers=SAME_ORIGIN_HEADERS)

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

    response = web_login.app.test_client().get("/api/status", headers=SAME_ORIGIN_HEADERS)

    assert response.get_json() == {
        "status": "pending",
        "auth_count": 1,
        "message": "等待扫码...",
    }
    assert response.headers["Cache-Control"] == "no-store"


def test_qr_response_is_not_cacheable(monkeypatch, tmp_path):
    monkeypatch.setattr(web_login, "_get_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(web_login, "screenshot_qr_code", lambda _: ("base64", "ok"))

    response = web_login.app.test_client().get("/api/qr", headers=SAME_ORIGIN_HEADERS)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


def test_stop_endpoint_is_not_available():
    response = web_login.app.test_client().post("/api/stop")

    assert response.status_code == 404


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

    response = web_login.app.test_client().get(
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
