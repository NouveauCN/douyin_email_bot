# AGENTS.md - Douyin Email Bot

Repository instructions for coding agents. They apply to the whole repository.

## Project And Files

This Python service polls IMAP for Douyin or Bilibili links, downloads media,
and replies over SMTP. It also includes Firefox cookie acquisition, a QR login
service on port 8080, and a trusted-LAN file browser on port 8081.
An optional standalone Node.js QQ C2C gateway accepts links through an
authenticated internal bridge.

```text
main.py                 Bot entry point and order-sensitive F2 bootstrap
email_bot.py            IMAP intake adapter, SMTP projector, lifecycle control
download_types.py       Public task/result/source contracts
task_store.py           Generic task facade over the durable SQLite store
download_task_service.py Registry, stateless executor, leases, retry workers
mail_state.py           SQLite mailbox position, task leases, and SMTP outbox
settings_store.py       SQLite managed runtime configuration and revisions
migrate_mail_state.py   Dry-run/apply migration for legacy JSON retries
douyin_downloader.py    F2 metadata and direct httpx media downloads
bilibili_downloader.py  Isolated yutto CLI wrapper
media_processor.py      Conservative shared image/video edge-border removal
process_media.py        Dry-run/apply CLI for existing downloaded media
url_extractor.py        Supported URL extraction
config_loader.py        YAML/env configuration dataclasses
cookie_extractor.py     Persistent Playwright Firefox cookie handling
get_cookie.py           Interactive/headless cookie CLI
web_login.py            Flask QR login service
file_browser.py         Flask browser, playlist, upload, dedup, and delete UI
play.py                 Local shuffled MP4 player
migrate_downloads.py    One-shot slideshow layout migration
smoke_download.py       Live Douyin smoke download
config.yaml             Non-secret runtime configuration
Dockerfile              Python 3.12 image with FFmpeg, Playwright, and yutto
docker-compose.yml      bot, web_login, and file_browser services
qq_gateway/              Node.js QQ C2C gateway (optional, single instance)
qq_bridge.py             Authenticated internal bridge and QQ outbox HTTP API
```

Main flow:

```text
IMAP -> EmailBot -> UrlExtractor -> DownloadTaskService -> platform adapter
                                              -> task event -> email outbox -> SMTP reply
QQ C2C -> qq_gateway -> authenticated qq_bridge -> DownloadTaskService
                                              -> task event -> QQ outbox reply
```

## Safety And Boundaries

- Never commit credentials, cookies, yutto auth files, Firefox profiles,
  downloaded media, or logs. Secrets belong in the managed settings volume or
  the gitignored `.env` bootstrap file.
- Preserve unrelated user changes in a dirty worktree.
- Do not casually run `smoke_download.py`: it uses a hardcoded live URL, valid
  credentials, network access, and the configured download destination.
- Network, IMAP, SMTP, browser, and download failures must be logged and handled
  without terminating the long-running poll loop.
- QQ is an optional C2C-only entry point. Keep the gateway on one instance,
  restrict it to explicit OpenIDs, and expose only its internal bridge; never
  publish the bridge port or accept wildcard allowlists.
- Preserve `_safe_subpath()` checks around every `file_browser.py` route that
  accepts a user path.
- Destructive file-browser routes must reject any path that resolves to the
  download root itself.
- `file_browser.py` intentionally has no application login for the personal
  deployment, but it is writable: upload, delete, crop, and duplicate-
  resolution endpoints modify the download tree. Keep its Docker host ports
  limited to loopback plus the explicitly configured trusted home-LAN address
  and restricted Tailscale ACL/Serve path; do not bind all interfaces or treat
  guest Wi-Fi, IoT networks, or Funnel as trusted.
- `web_login.py` also has no application login by design. Its Docker host port
  must remain limited to loopback plus the explicitly configured trusted
  home-LAN address, its API must enforce same-origin/allowed-origin checks and
  QR/status rate limits, and Tailscale policy must restrict access to the
  intended user or devices.
- The file browser reads `/app/comics/pics` as a separate read-only comics
  gallery source. Its `/comics/raw/...` and `/comics/image/...` routes must
  validate resolved paths within that source and must never pass comics paths
  to download upload, delete, crop, or dedup operations.

## F2 Bootstrap Invariant

`main.py` must patch F2 before importing `email_bot.py` or downloader code. It:

- writes minimal F2/Bark configuration;
- makes Douyin browser-model accessors return dictionaries;
- forces a host-appropriate Firefox/Gecko fingerprint to match the persistent
  Firefox cookies;
- falls back from real to false `msToken` generation;
- tolerates empty Bark configuration.

Moving F2-dependent imports above this bootstrap reproduces import-time crashes
or HTTP 200 responses with empty Douyin data. `f2_bootstrap.py` is the shared
bootstrap used by both entry points; keep it before any F2-dependent imports.

## Runtime Invariants

### Email processing

- Poll IMAP over SSL for `UNSEEN`, skip the bot's own mail, and deduplicate IMAP
  IDs in memory.
- Apply the optional sender allowlist before normal download routing. Cookie
  updates are handled by Web Login or the CLI, never by an email body.
- Cooldown is per sender and is set after a successful download.
- Douyin download failures must point operators to Web Login or
  `get_cookie.py`; the mail adapter must not refresh Firefox implicitly.
- Persist transient network/timeout failures in the configured retry queue;
  exhausted links go to the configured failure file.
- Durable terminal failure projection is idempotent by `task_id`. During
  upgrades, only an already-consumed event may reuse an exact matching
  unkeyed legacy failure row (same sender, platform, URL, and error), so
  replay does not duplicate an old record; new terminal rows always include
  `task_id`.
- In Docker, keep the retry queue and failure file under the bot's named
  `state` volume (`/app/state`); do not rely on the container writable layer.
- Cache successful `v.douyin.com` resolutions so later attempts can use the
  aweme ID without repeating flaky redirects.
- Resolve `v.douyin.com` short links over verified HTTPS only. Accept redirects
  only from the approved Douyin hosts and exact video/note ID paths; never
  cache HTTP, foreign-host, malformed, or certificate-invalid targets. A
  private CA is allowed only through an explicit CA-bundle setting.
- `_safe_logout()` closes the socket directly; do not restore blocking IMAP
  protocol logout after a broken connection.
- In the legacy path, allowlist- and keyword-skipped mail is initially left
  unseen but already present in `_seen_ids`; the next poll normally marks it
  seen. A restart before then can evaluate it again. Durable intake records
  these routes as complete and ACKs them; cooldown is represented by queued
  work rather than an intake skip.
- If a UIDVALIDITY change strands an incomplete source from an older mailbox
  generation, the source is retained and rollback remains blocked; do not
  auto-ack or discard it. An operator must restore/reconcile the mailbox or
  explicitly resolve the source before switching to the legacy path.
- Durable mail processing is enabled by default. The IMAP coordinator uses
  `UID SEARCH`/`UID FETCH`, persists `(mailbox, UIDVALIDITY, UID)` in SQLite,
  and marks `\\Seen` only after source routing is marked complete.
- Download tasks and SMTP notifications run in bounded daemon workers with
  SQLite leases and heartbeats. Expired leases are recovered by an independent
  maintenance scheduler; the outbox reuses a stable Message-ID.
- `EMAIL_SEND_REPLIES=0` is a temporary fail-safe for silent processing: intake,
  downloads, retries, terminal state, and failure records continue, terminal
  events are consumed without creating new SMTP outbox rows, and the SMTP
  worker is not started. Existing outbox rows are preserved for a later run
  with replies enabled.
- The old JSON retry queue remains as a rollback source and is imported
  idempotently at startup. `BOT_DURABLE_MAIL_ENABLED=0` selects the legacy
  polling/retry path only after SQLite intake, pending `\\Seen` acknowledgements,
  tasks, and outbox work are drained; startup refuses this mode when unfinished
  durable work remains.
- `DownloadTaskService` owns bounded download workers, platform capacity,
  leases, heartbeats, retries, and durable completion events. `EmailBot` owns
  IMAP intake and its email-specific event projector/outbox. The legacy path
  calls the stateless `DownloadExecutor` synchronously and does not start the
  durable service.
- Firefox cookie extraction remains process-serialized; Douyin and Bilibili
  worker capacity is independently bounded by bot configuration.

### QQ gateway

- The Node gateway accepts only C2C messages from `QQBOT_ALLOWED_OPENIDS` and
  submits messages through the authenticated `QQ_BRIDGE_TOKEN` bridge at
  `http://bot:8082`; group/channel messages and unauthorized OpenIDs never enter
  the download service.
- A QQ message must contain exactly one supported Douyin/Bilibili URL. The
  bridge deduplicates the message, queues an immediate acknowledgement, and
  projects the terminal task result to the QQ outbox idempotently.
- QQ replies are passive replies only. `QQBOT_REPLY_WINDOW_SECONDS` defaults to
  3600 seconds; an expired item is retained as a delivery failure and must not
  be converted into an unsolicited proactive message.
- The gateway stores its session/outbox delivery state in the independent
  `qq_gateway_state` volume. Credentials and bridge tokens belong only in the
  untracked `.env`; do not log App Secrets, cookies, or server paths.

### Douyin downloads

- `DouyinDownloader.download()` is synchronous externally and uses
  `asyncio.run()` internally; metadata comes from F2 and media from `httpx`.
- Regular videos use
  `<root>/<author>/<YYYYMMDD_HHMMSS>_<aweme_id>.mp4` when folderized.
- Static slideshow images go to `<root>/slides/`; animated MP4 clips follow the
  author-folder layout. Extension detection is heuristic and defaults to WebP.
- Slideshow retries reuse completed items by aweme ID and item index even when
  the timestamp prefix changes, so partial-success retries do not duplicate files.
- Downloaded images, regular videos, and animated clips pass through the shared
  `media_processor.py` edge-crop pipeline. Post-processing failures must not
  turn successful downloads into failures.
- Media downloads stream into unique same-directory temporary files and replace
  the destination atomically; empty files are never treated as successful.
- `douyin.max_tasks` is configured but single downloads currently force one
  task.

### Bilibili downloads

- Keep yutto as a subprocess CLI. It is isolated in `/opt/yutto` in Docker
  because its dependencies conflict with F2.
- Preserve mp4 output, configured quality, `hevc,avc,av1` preference, and the
  current no-danmaku/no-subtitle/no-progress/no-color behavior.
- Move cover sidecars to the sibling `slides/` directory with a `bilibili_`
  prefix; covers must not count as video results.
- Run newly downloaded Bilibili videos and moved covers through the shared
  media processor without changing `files`, `covers`, or count metadata.
- One URL may return multiple files. Preserve `files` and `file_count` metadata
  and useful single- and multi-file email replies.

### Media post-processing

- Remove only consecutive near-uniform rows or columns connected to an outside
  edge. Never remove internal lines, and never use darkness alone as a border
  signal.
- Keep the strict pixel coverage and 90% whole-duration video-frame consensus.
  Standard crops retain the conservative per-side and area limits. Extended
  crops may auto-apply only when every sampled frame supports stable, paired
  opposite edges; otherwise they must return `requires_review`.
- Successful crops keep the source as `<stem>_original.bak`. Image writes are
  temporary and atomically replace the destination; all failures restore the
  source.
- The bot checks media backups at startup and every seven days. It deletes only
  `*_original.bak` files that have been retained for at least 28 days; retention
  and check intervals are configurable under `media_cleanup`.
- H.264 crop dimensions must remain even. Preserve audio by stream copy and
  prefer the reported source video bitrate so re-encoding does not imply or
  waste space on nonexistent quality improvements.
- `process_media.py` is dry-run by default; existing downloads change only with
  explicit `--apply`. Review candidates additionally require `--force-review`.
- The trusted-LAN file browser video page exposes preview/apply endpoints for
  manual review. Every media path accepted by these routes must pass through
  `_safe_subpath()`.

### Cookies

- Cookie acquisition is Firefox-only and uses a persistent Playwright profile.
- First login may be interactive; later extraction reuses the profile headlessly.
- QR generation opens the Douyin login dialog, captures the complete viewport,
  and serializes Firefox access between QR and status requests.
- QR status responses must never expose cookie contents to the browser and must
  remain non-cacheable; successful cookies are persisted server-side only.
- Keep auth-cookie indicators aligned between `cookie_extractor.py` and
  `douyin_downloader.py`.
- `.env` update helpers currently write in place rather than atomically. Keep
  their formatting consistent and prefer a shared atomic implementation when
  changing them.
- Web Login and `get_cookie.py` remain the only cookie acquisition entry points.

### Browser settings control plane

- `file_browser` exposes a Settings tab for supported email, sender allowlist,
  keyword, downloader, retry, media, and the managed Douyin Cookie secret;
  email Cookie commands have been removed and are not settings.
- Managed settings are persisted in the dedicated `runtime_settings` volume at
  `/app/runtime-settings/settings.sqlite3`; it is shared only by `bot`,
  `file_browser`, and `web_login`. `file_browser` must never receive the bot's
  `/app/state` volume or Docker socket.
- Secret values (mail credentials, Douyin Cookie, Bilibili auth) are write-only
  in the UI: responses, logs, and errors may report only configured/unconfigured
  status. `config.yaml` remains read-only.
- Settings `PATCH` requests require an explicitly configured
  `FILE_BROWSER_ALLOWED_ORIGINS`; when it is missing the endpoint must return
  `403`, including for otherwise same-origin requests. Use exact origins only.
- Cookie-only changes hot-reload in the bot. Other changes request a graceful
  restart: stop intake and new claims, drain active work for at most 300 seconds,
  then exit so Docker `restart: unless-stopped` starts the bot automatically.
  Interrupted long Bilibili work is recovered by SQLite lease expiry.

## Configuration And Paths

For supported overrides, priority is:

```text
environment > managed settings > legacy .env > config.yaml > dataclass default
```

The environment layer means only variables explicitly injected by the
deployment/operator; Compose does not provide default environment overrides for
editable worker, lease/heartbeat, or SMTP outbox settings. Those are controlled
by managed settings or `config.yaml` unless an external process variable is
deliberately supplied, in which case the field is locked in the Settings tab.

`config_loader.py` is the source of truth for environment-variable mappings.
Sensitive variables include `EMAIL_ADDRESS`, `EMAIL_PASSWORD`, `DOUYIN_COOKIE`,
`BILIBILI_AUTH`, `QQBOT_APP_SECRET`, and `QQ_BRIDGE_TOKEN`;
`BILIBILI_AUTH_FILE` may reference sensitive login state. `QQBOT_APP_ID` and
`QQBOT_ALLOWED_OPENIDS` are deployment configuration and should remain in the
untracked `.env` when possible.
`EMAIL_SEND_REPLIES` controls SMTP result notifications and defaults to enabled;
it does not disable mail intake or downloads. `SENDER_ADDRESS` and
`SENDER_PASSWORD` are test-driver-only credentials and must not be added to
production `EmailConfig` or persisted in managed settings.
Relative configured paths resolve against the directory containing
`config.yaml`, not the process working directory.

`BOT_TRANSIENT_PENDING_FILE` and `BOT_TRANSIENT_FAILED_FILE` override the
transient retry queue and exhausted-link file paths. The Docker bot sets them
to `/app/state/pending_retries.json` and `/app/state/failed_links.txt`.
`BOT_STATE_DB` defaults to `/app/state/mail_state.sqlite3` in Docker; it must
remain on the bot's persistent `state` volume and never on the NAS media mount.
The state database is a `0600` runtime artifact. On upgrade from the pre-v2
state schema, any legacy `platform=cookie` task is redacted and moved to
terminal failure so the user must use Web Login or the CLI; migration enables
SQLite secure-delete and VACUUM/checkpoints the database/WAL before workers
start. Completion events have per-consumer acknowledgements; an unconsumed
email event blocks legacy rollback until the email projector catches up.

The checked-in config points downloads directly at
`/srv/nas_data/douyin_downloads`. Docker overrides that host path with
`/app/downloads`. `smoke_download.py` and `migrate_downloads.py` follow the
configured path, while `play.py` ignores `config.yaml` and defaults to the
checkout-local `./downloads/`. Pass the NAS path explicitly when using it on the
deployment host.

The short-link cache defaults to `logs/short_link_cache.json` and can be moved
with `DOUYIN_SHORT_LINK_CACHE`. Cache, retry, failure, media, log, and profile
artifacts must remain untracked.

The browser Settings tab writes managed values to the runtime-settings database;
the legacy `.env` bind remains for bootstrap, compatibility, and Compose
interpolation. Environment variables intentionally supplied by the deployment
remain the highest-priority, read-only overrides in the UI.

## Development And Verification

```bash
uv sync --frozen
uv run python main.py
uv run python web_login.py
uv run python file_browser.py
uv run python play.py --dry-run --download-dir /srv/nas_data/douyin_downloads
uv run python get_cookie.py
uv run python get_cookie.py --headless
uv run python migrate_downloads.py --dry-run
uv run python process_media.py /srv/nas_data/douyin_downloads
uv run python migrate_mail_state.py --pending ./pending_retries.json
uv run python migrate_mail_state.py --pending ./pending_retries.json --apply
```

Verification baseline:

```bash
git diff --check
uv run python -m compileall .
docker compose --profile login config --quiet
```

Documentation-only changes need `git diff --check`. Python changes need at least
affected-module compilation and focused tests when practical. Mock IMAP, SMTP,
browser, network, and filesystem side effects; do not use live downloads as a
routine test.

## Change Delivery

- Every completed modification round must be committed with a clear,
  descriptive message that summarizes the full change; avoid vague messages
  such as `update` or `fix`.
- Do not leave completed work only in the local checkout. After verification,
  push a branch, open a PR, and merge it into GitHub `main`.
- Use local `git` for status, staging, commits, and pushes. Use the authenticated
  GitHub CLI (`gh`) by default for PR creation, inspection, readiness, and merge
  operations.
- After merging a functional code, dependency, configuration, or runtime change,
  sync local `main` and rebuild/restart the affected Docker services without
  waiting for a separate request. Verify container status afterward.
- Documentation-only changes do not require a container rebuild. Keep
  profile-only services such as `web_login` stopped unless they are needed; if
  they changed, rebuild the profile image without leaving it running.

## Docker Deployment

```bash
sudo docker compose up -d --build bot qq_gateway file_browser
sudo docker compose --profile login up web_login
sudo docker compose down
```

- The bot owns the `logs` and `state` volumes; `state` persists
  `pending_retries.json` and `failed_links.txt` across bot container rebuilds.
- `qq_gateway` is a single-instance optional service built from `./qq_gateway`.
  It depends on `bot`, has no published host ports, and stores SDK/session and
  delivery state in the independent `qq_gateway_state` volume. The bot's bridge
  listens on `0.0.0.0:8082` inside the Compose network only; both services must
  receive the same `QQ_BRIDGE_TOKEN`.
  Bot and `web_login` share the Firefox-profile volume.
- `runtime_settings` is an independent named volume mounted at
  `/app/runtime-settings` in `bot`, `file_browser`, and `web_login`, with
  `RUNTIME_SETTINGS_DB=/app/runtime-settings/settings.sqlite3`. It is not the
  mail state volume and is never mounted into unrelated services.
- Bot and `file_browser` bind the host NAS root to `/app/downloads`.
- `file_browser` also mounts `/srv/nas_data/comics` read-only at `/app/comics`
  and uses `COMICS_PICS_PATH=/app/comics/pics` for the in-site comics gallery.
- All services bind `config.yaml` read-only; only bot and `web_login` bind the
  legacy `.env` for compatibility. Managed settings are the normal mutable
  control-plane source.
- `file_browser` also receives the legacy `.env` as read-only bootstrap input;
  it must not write that file. The bot Compose environment must not reintroduce
  default `BOT_WORKER_COUNT`, `BOT_DOUYIN_WORKER_COUNT`,
  `BOT_BILIBILI_WORKER_COUNT`, `BOT_LEASE_SECONDS`, `BOT_HEARTBEAT_SECONDS`, or
  `BOT_OUTBOX_*` values: these remain managed/YAML settings unless explicitly
  injected outside Compose.
- The bot intentionally clears proxy variables so Douyin traffic goes direct.
- Python 3.12 is required locally and in Docker. `pyproject.toml` declares the
  exact direct dependencies, `uv.lock` is the authoritative transitive lock,
  and Docker installs it with `uv sync --frozen`; do not restore a parallel
  hand-maintained `requirements.txt`. Keep yutto 2.2.0 and its transitive
  dependencies frozen by `dependency-locks/yutto/uv.lock` and isolated in
  `/opt/yutto` because its dependency set conflicts with F2.
- The deployment checkout is `~/douyin_email_bot` on `nouveau@nouveauserver`.
  NAS writes outside Docker may require `sudo`.
- When editing elsewhere, push first, then pull on the server and rebuild the
  affected services. When Codex is already in the server checkout, edit and
  verify there, then rebuild as needed.

## Known Gaps

- The prioritized remediation and upgrade sequence is tracked in `ROADMAP.md`.
- The thumbnail cache is fixed at `/app/.thumb_cache`.
- Flask HTML, CSS, and JavaScript remain inline in Python modules.

Any substantial change to architecture, media layout, configuration,
dependencies, or startup/deployment behavior must update this file.
