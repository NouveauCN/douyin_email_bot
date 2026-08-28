"""Focused tests for the file-browser settings API and settings tab."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import file_browser
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
        self.assertIn('id="settingsGroups"', page)
        self.assertIn("function renderSettings", page)
        self.assertIn("恢复默认", page)


if __name__ == "__main__":
    unittest.main()
