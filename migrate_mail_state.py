"""Migrate legacy JSON retry records into the durable SQLite task store.

The command is intentionally dry-run by default.  ``--apply`` imports records
idempotently and leaves the JSON file untouched, so the previous bot version
can still be used as a rollback path until durable work is verified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from mail_state import MailStateStore


def import_pending_retries(
    pending_path: str | Path,
    state: MailStateStore,
    *,
    apply: bool = True,
) -> dict[str, int]:
    """Import valid legacy records and report skipped/malformed entries."""
    path = Path(pending_path)
    if not path.exists():
        return {"total": 0, "imported": 0, "skipped": 0}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read pending retry file {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"pending retry file {path} must contain an object")

    report = {"total": len(raw), "imported": 0, "skipped": 0}
    for key, item in raw.items():
        if not isinstance(item, dict) or not str(item.get("url") or "").strip():
            report["skipped"] += 1
            continue
        if not apply:
            report["imported"] += 1
            continue
        source_id = "legacy-retry:" + hashlib.sha256(str(key).encode("utf-8")).hexdigest()
        state.enqueue_task(
            source_id,
            str(item["url"]).strip(),
            payload={
                "sender": item.get("sender", ""),
                "subject": item.get("subject", ""),
                "legacy_retry_key": str(key),
                "legacy_attempts": item.get("attempts", 0),
            },
            platform=item.get("platform"),
        )
        report["imported"] += 1
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pending", type=Path, default=Path("./pending_retries.json"))
    parser.add_argument("--state-db", type=Path, default=Path("./state/mail_state.sqlite3"))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write imported tasks to SQLite (otherwise only validate/count)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.apply:
        with MailStateStore(args.state_db) as state:
            report = import_pending_retries(args.pending, state, apply=True)
    else:
        # Dry-run still validates records, using an in-memory store so no
        # filesystem state is created.
        with MailStateStore(":memory:") as state:
            report = import_pending_retries(args.pending, state, apply=False)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
