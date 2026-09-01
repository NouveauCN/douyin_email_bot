"""Focused tests for the file-browser settings API and settings tab."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import file_browser
import web_login
from settings_store import SettingsStore


class FileBrowserSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = SettingsStore(Path(self.tempdir.name) / "settings.sqlite3")
        self.store_patch = patch.object(file_browser, "_SETTINGS_STORE", self.store)
        self.store_patch.start()
        self.origin_mode_patch = patch.object(file_browser, "_USE_DEFAULT_ORIGIN", False)
        self.origins_patch = patch.object(file_browser, "_ALLOWED_ORIGINS", ("http://localhost",))
        self.origin_mode_patch.start()
        self.origins_patch.start()
        self.client = file_browser.app.test_client()
        self.client.environ_base["HTTP_ORIGIN"] = "http://localhost"

    def tearDown(self):
        self.store_patch.stop()
        self.origins_patch.stop()
        self.origin_mode_patch.stop()
        self.tempdir.cleanup()

    def test_settings_snapshot_is_grouped_and_redacts_secrets(self):
        response = self.client.get("/api/settings")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("email", payload["groups"])
        self.assertIn("douyin", payload["groups"])
        self.assertIsNone(payload["groups"]["email"]["password"]["value"])
        for group in payload["groups"].values():
            for field in group.values():
                self.assertTrue(field["label"])
                self.assertTrue(field["description"])
                self.assertIn("unit", field)
                self.assertIn("example", field)
                self.assertTrue(field["value_type"])
        browser = payload["groups"]["file_browser"]
        self.assertEqual(len(browser), 7)
        self.assertEqual(browser["settings_database"]["label"], "设置数据库路径")
        self.assertTrue(all(not field["editable"] for field in browser.values()))
        self.assertTrue(all(field["apply_mode"] == "readonly" for field in browser.values()))

    def test_settings_snapshot_never_returns_secret_value_or_secret_fragments(self):
        revision = self.client.get("/api/settings").get_json()["revision"]
        response = self.client.patch(
            "/api/settings",
            json={
                "base_revision": revision,
                "changes": [{"key": "douyin.cookie", "action": "set", "value": "super-secret-cookie"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = self.client.get("/api/settings").get_json()
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("super-secret-cookie", serialized)
        self.assertIsNone(payload["groups"]["douyin"]["cookie"]["value"])
        self.assertTrue(payload["groups"]["douyin"]["cookie"]["configured"])

    def test_cookie_update_is_immediate_and_blank_secret_is_ignored(self):
        revision = self.client.get("/api/settings").get_json()["revision"]
        response = self.client.patch(
            "/api/settings",
            json={
                "base_revision": revision,
                "changes": [{"key": "douyin.cookie", "action": "set", "value": "cookie-value"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["restart"])
        next_revision = response.get_json()["revision"]

        ignored = self.client.patch(
            "/api/settings",
            json={
                "base_revision": next_revision,
                "changes": [{"key": "douyin.cookie", "action": "set", "value": ""}],
            },
        )
        self.assertEqual(ignored.status_code, 200)
        self.assertEqual(self.store.get("douyin.cookie"), "cookie-value")

    def test_restart_setting_returns_request_id(self):
        revision = self.client.get("/api/settings").get_json()["revision"]
        response = self.client.patch(
            "/api/settings",
            json={
                "base_revision": revision,
                "changes": [{"key": "bot.subject_keyword", "action": "set", "value": "保存"}],
            },
        )
        self.assertEqual(response.status_code, 202)
        request_id = response.get_json()["request_id"]
        self.store.update_restart(request_id, "draining", active_count=3)
        status = self.client.get("/api/settings/restart/" + request_id)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()["status"], "draining")
        self.assertEqual(status.get_json()["active_count"], 3)
        self.assertEqual(self.client.get("/api/settings").get_json()["restart"]["status"], "draining")

    def test_invalid_readonly_and_revision_conflict(self):
        revision = self.client.get("/api/settings").get_json()["revision"]
        readonly = self.client.patch(
            "/api/settings",
            json={"base_revision": revision, "changes": [{"key": "douyin.download_path", "action": "set", "value": "/tmp/x"}]},
        )
        self.assertEqual(readonly.status_code, 403)
        invalid = self.client.patch(
            "/api/settings",
            json={"base_revision": revision, "changes": [{"key": "email.imap_port", "action": "set", "value": "bad"}]},
        )
        self.assertEqual(invalid.status_code, 400)
        conflict = self.client.patch(
            "/api/settings",
            json={"base_revision": revision - 1, "changes": [{"key": "bot.subject_keyword", "action": "set", "value": "x"}]},
        )
        self.assertEqual(conflict.status_code, 409)

    def test_settings_patch_requires_explicit_allowlist(self):
        with patch.object(file_browser, "_USE_DEFAULT_ORIGIN", True):
            response = self.client.patch(
                "/api/settings",
                json={"base_revision": 0, "changes": []},
            )
        self.assertEqual(response.status_code, 403)

    def test_settings_patch_rejects_dns_rebinding_host(self):
        response = self.client.patch(
            "/api/settings",
            json={"base_revision": 0, "changes": []},
            headers={"Origin": "http://localhost", "Host": "evil.example"},
        )
        self.assertEqual(response.status_code, 403)

    def test_settings_patch_requires_base_revision(self):
        response = self.client.patch("/api/settings", json={"changes": []})
        self.assertEqual(response.status_code, 400)

    def test_settings_patch_has_independent_json_size_limit(self):
        body = json.dumps({"base_revision": 0, "changes": [{"key": "bot.subject_keyword", "action": "set", "value": "x" * (1024 * 1024)}]})
        response = self.client.patch(
            "/api/settings",
            data=body,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 413)

    def test_home_contains_settings_tab_and_dynamic_renderer(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="settingsTab"', page)
        self.assertIn('id="webLoginTab"', page)
        self.assertIn('id="webLoginUnlockForm"', page)
        self.assertIn("var api = '/api/web-login'", page)
        self.assertIn("function responseData(response)", page)
        self.assertIn("FILE_BROWSER_ALLOWED_ORIGINS 包含当前访问地址", page)
        self.assertIn("loadQr();", page)
        self.assertIn('id="settingsGroups"', page)
        self.assertIn("function renderSettings", page)
        self.assertIn("field.label", page)
        self.assertIn("field.description", page)
        self.assertIn("立即生效", page)
        self.assertIn("保存后自动重启 Bot", page)
        self.assertIn("只读部署项", page)
        self.assertIn("option.textContent = item[1]", page)
        self.assertIn("恢复默认", page)

    def test_embedded_web_login_requires_unlock_before_qr(self):
        with patch.dict("os.environ", {"WEB_LOGIN_PASSWORD": "test-password"}):
            with patch.object(web_login, "screenshot_qr_code", return_value=("base64", "ok")) as screenshot:
                locked = self.client.get("/api/web-login/qr")
                self.assertEqual(locked.status_code, 401)
                unlocked = self.client.post(
                    "/api/web-login/unlock",
                    json={"password": "test-password"},
                )
                self.assertEqual(unlocked.status_code, 200)
                response = self.client.get("/api/web-login/qr")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        screenshot.assert_called_once()


if __name__ == "__main__":
    unittest.main()
