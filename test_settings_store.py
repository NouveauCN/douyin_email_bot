import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from settings_store import SETTING_REGISTRY, SettingsStore, default_database_path, read_dotenv, setting_registry


def test_dotenv_is_read_without_polluting_environment(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("EMAIL_ADDRESS='bot@example.com'\nexport DOUYIN_COOKIE=abc # note\n", encoding="utf-8")
    monkeypatch.delenv("EMAIL_ADDRESS", raising=False)
    assert read_dotenv(dotenv) == {"EMAIL_ADDRESS": "bot@example.com", "DOUYIN_COOKIE": "abc"}
    assert "EMAIL_ADDRESS" not in os.environ


def test_store_revision_validation_reset_and_secret_redaction(tmp_path, monkeypatch):
    monkeypatch.delenv("EMAIL_ADDRESS", raising=False)
    store = SettingsStore(tmp_path / "settings.sqlite3")
    assert store.get_revision() == 0
    revision = store.apply_changes({"email.email": "bot@example.com", "email.password": "password", "douyin.cookie": "secret"}, 0)
    assert revision == 1
    assert store.get("douyin.cookie") == "secret"
    assert store.snapshot()["douyin.cookie"]["value"] is None
    assert store.snapshot()["douyin.cookie"]["configured"] is True
    with pytest.raises(RuntimeError, match="revision conflict"):
        store.apply_changes({"email.email": "other@example.com"}, 0)
    assert store.reset(["douyin.cookie"], revision) == 2
    assert store.get("douyin.cookie") is None


def test_environment_override_makes_setting_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("EMAIL_ADDRESS", "from-env@example.com")
    store = SettingsStore(tmp_path / "settings.sqlite3")
    assert store.snapshot()["email.email"]["source"] == "env"
    assert store.snapshot()["email.email"]["editable"] is False
    with pytest.raises(PermissionError):
        store.apply_changes({"email.email": "from-ui@example.com"}, 0)


def test_restart_and_heartbeat_lifecycle(tmp_path):
    store = SettingsStore(tmp_path / "settings.sqlite3")
    request_id = store.request_restart()
    assert store.restart_status(request_id)["status"] == "queued"
    claimed = store.claim_queued_restart()
    assert claimed["request_id"] == request_id
    assert claimed["status"] == "draining"
    store.update_restart(request_id, "draining", active_count=2, detail="waiting")
    assert store.restart_status(request_id)["active_count"] == 2
    assert store.restart_status(request_id)["detail"] == "waiting"
    assert store.claim_queued_restart() is None
    store.heartbeat("bot", boot_id="boot-1", revision=0)
    assert store.heartbeat_status()["boot_id"] == "boot-1"


def test_default_database_path_honors_runtime_override(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_SETTINGS_DB", str(tmp_path / "runtime.sqlite3"))
    assert default_database_path(tmp_path / "config.yaml") == tmp_path / "runtime.sqlite3"
    assert "email.email" in setting_registry()


def test_apply_and_maybe_restart_is_one_transaction(tmp_path):
    store = SettingsStore(tmp_path / "settings.sqlite3")
    result = store.apply_and_maybe_restart(
        [{"key": "douyin.cookie", "action": "set", "value": "cookie"}],
        base_revision=0,
    )
    assert result == {"revision": 1, "request_id": None, "apply_mode": "hot"}
    result = store.apply_and_maybe_restart(
        [{"key": "bot.subject_keyword", "action": "set", "value": "save"}],
        base_revision=1,
    )
    assert result["revision"] == 2
    assert result["request_id"]
    assert store.restart_status(result["request_id"])["revision"] == 2


def test_clear_only_secrets_and_control_characters_are_rejected(tmp_path):
    store = SettingsStore(tmp_path / "settings.sqlite3")
    with pytest.raises(ValueError, match="only supported"):
        store.apply_and_maybe_restart([{"key": "bot.subject_keyword", "action": "clear"}])
    with pytest.raises(ValueError, match="control characters"):
        store.apply_and_maybe_restart([{"key": "douyin.cookie", "action": "set", "value": "a\nsecret"}])


def test_clear_and_reset_cannot_remove_last_email_credential(tmp_path):
    store = SettingsStore(tmp_path / "settings.sqlite3")
    store.apply_and_maybe_restart([
        {"key": "email.email", "action": "set", "value": "bot@example.com"},
        {"key": "email.password", "action": "set", "value": "password"},
    ])
    with pytest.raises(ValueError, match="both be configured"):
        store.apply_and_maybe_restart([{"key": "email.password", "action": "clear"}], base_revision=1)
    with pytest.raises(ValueError, match="both be configured"):
        store.reset(["email.password"], base_revision=1)
    assert store.get("email.password") == "password"


def test_email_ports_have_strict_bounds(tmp_path):
    store = SettingsStore(tmp_path / "settings.sqlite3")
    store.apply_and_maybe_restart([
        {"key": "email.email", "action": "set", "value": "bot@example.com"},
        {"key": "email.password", "action": "set", "value": "password"},
    ])
    with pytest.raises(ValueError, match="port"):
        store.apply_and_maybe_restart([{"key": "email.imap_port", "action": "set", "value": 0}])
    with pytest.raises(ValueError, match="port"):
        store.apply_and_maybe_restart([{"key": "email.smtp_port", "action": "set", "value": 65536}])
    store.apply_and_maybe_restart([{"key": "email.imap_port", "action": "set", "value": 1}])
    assert store.get("email.imap_port") == 1


def test_secret_values_are_never_in_snapshot_or_public_managed_values(tmp_path):
    store = SettingsStore(tmp_path / "settings.sqlite3")
    store.apply_and_maybe_restart([{"key": "douyin.cookie", "action": "set", "value": "top-secret"}])
    metadata = store.snapshot()
    assert metadata["douyin.cookie"]["value"] is None
    assert metadata["douyin.cookie"]["configured"] is True
    assert "douyin.cookie" not in store.managed_values(include_secrets=False)


def test_resource_limits_cover_strings_secrets_and_allowlist(tmp_path):
    store = SettingsStore(tmp_path / "settings.sqlite3")
    with pytest.raises(ValueError, match="too long"):
        store.apply_and_maybe_restart([{"key": "bot.subject_keyword", "action": "set", "value": "x" * 4097}])
    with pytest.raises(ValueError, match="too long"):
        store.apply_and_maybe_restart([{"key": "douyin.cookie", "action": "set", "value": "x" * (1_048_576 + 1)}])
    with pytest.raises(ValueError, match="200"):
        store.apply_and_maybe_restart([{"key": "bot.allowed_senders", "action": "set", "value": [f"{n}@example.com" for n in range(201)]}])
    with pytest.raises(ValueError, match="item"):
        store.apply_and_maybe_restart([{"key": "bot.allowed_senders", "action": "set", "value": ["x" * 321]}])


def test_no_base_concurrent_email_writes_cannot_create_partial_credentials(tmp_path):
    def write(key, value):
        return SettingsStore(tmp_path / "settings.sqlite3").apply_and_maybe_restart(
            [{"key": key, "action": "set", "value": value}]
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda item: _capture_error(write, *item),
            [("email.email", "bot@example.com"), ("email.password", "password")],
        ))
    assert all(result is ValueError for result in results)
    assert SettingsStore(tmp_path / "settings.sqlite3").managed_values() == {}


def _capture_error(function, *args):
    try:
        function(*args)
    except Exception as exc:  # noqa: BLE001 - test records validation class
        return type(exc)
    return None


def test_snapshot_reports_all_sources_and_effective_nonsecret_values(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("email:\n  smtp_timeout: 44\nbot:\n  allowed_senders: [yaml@example.com]\n", encoding="utf-8")
    (tmp_path / ".env").write_text("EMAIL_SMTP_SERVER=legacy.example\n", encoding="utf-8")
    store = SettingsStore(tmp_path / "settings.sqlite3")
    store.apply_changes({"bot.subject_keyword": "managed"})
    monkeypatch.setenv("EMAIL_IMAP_PORT", "995")
    snapshot = store.snapshot(config_path=config)
    assert snapshot["email.imap_port"]["source"] == "env"
    assert snapshot["email.imap_port"]["value"] == 995
    assert snapshot["email.smtp_server"]["source"] == "legacy_env"
    assert snapshot["email.smtp_server"]["value"] == "legacy.example"
    assert snapshot["email.smtp_timeout"]["source"] == "yaml"
    assert snapshot["email.smtp_timeout"]["value"] == 44
    assert snapshot["bot.subject_keyword"]["source"] == "managed"
    assert snapshot["bot.subject_keyword"]["value"] == "managed"
    assert snapshot["bot.worker_count"]["source"] == "default"
    assert snapshot["bot.worker_count"]["value"] == 2


def test_every_registered_setting_has_safe_help_metadata_and_type(tmp_path):
    store = SettingsStore(tmp_path / "settings.sqlite3")
    snapshot = store.snapshot()

    assert len(SETTING_REGISTRY) == 45
    assert set(snapshot) == set(SETTING_REGISTRY)
    for key, definition in SETTING_REGISTRY.items():
        field = snapshot[key]
        assert definition.label.strip()
        assert definition.description.strip()
        assert field["label"] == definition.label
        assert field["description"] == definition.description
        assert field["unit"] == definition.unit
        assert field["example"] == definition.example
        assert field["input_hint"] == definition.input_hint
        assert field["value_type"] == definition.value_type


def test_secret_snapshot_redacts_values_without_metadata_leaks(tmp_path):
    store = SettingsStore(tmp_path / "settings.sqlite3")
    secret_changes = {
        key: (
            "sentinel-secret@example.com"
            if key == "email.email"
            else f"sentinel-secret-for-{key}"
        )
        for key, definition in SETTING_REGISTRY.items()
        if definition.secret and definition.editable
    }
    store.apply_changes(secret_changes)
    snapshot = store.snapshot()
    serialized = repr(snapshot)

    for key, definition in SETTING_REGISTRY.items():
        if definition.secret:
            assert snapshot[key]["value"] is None

    for key, secret in secret_changes.items():
        assert snapshot[key]["secret"] is True
        assert snapshot[key]["value"] is None
        assert snapshot[key]["configured"] is True
        assert secret not in serialized
        assert secret not in snapshot[key]["label"]
        assert secret not in snapshot[key]["description"]
        assert secret not in (snapshot[key]["example"] or "")
        assert secret not in (snapshot[key]["input_hint"] or "")
