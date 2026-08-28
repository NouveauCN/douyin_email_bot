import json

from mail_state import MailStateStore
from migrate_mail_state import import_pending_retries


def test_legacy_retry_import_is_dry_run_and_idempotent(tmp_path):
    pending = tmp_path / "pending.json"
    pending.write_text(
        json.dumps(
            {
                "sender\nhttps://example.test/a": {
                    "url": "https://example.test/a",
                    "sender": "user@example.test",
                    "subject": "下载",
                    "platform": "douyin",
                },
                "bad": {"attempts": 2},
            }
        ),
        encoding="utf-8",
    )
    with MailStateStore(tmp_path / "state.sqlite") as state:
        assert import_pending_retries(pending, state, apply=False) == {
            "total": 2,
            "imported": 1,
            "skipped": 1,
        }
        assert import_pending_retries(pending, state, apply=True) == {
            "total": 2,
            "imported": 1,
            "skipped": 1,
        }
        assert import_pending_retries(pending, state, apply=True) == {
            "total": 2,
            "imported": 1,
            "skipped": 1,
        }
        assert state._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert pending.exists()
