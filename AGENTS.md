# AGENTS.md - Douyin Email Bot

Repository instructions for coding agents. They apply to the whole repository.

## Project And Files

This Python service polls IMAP for Douyin or Bilibili links, downloads media,
and replies over SMTP. It also includes Firefox cookie acquisition, a QR login
service on port 8080, and a trusted-LAN file browser on port 8081.

```text
main.py                 Bot entry point and order-sensitive F2 bootstrap
email_bot.py            IMAP loop, routing, retries, SMTP, cookie refresh
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
test_download.py        Live Douyin smoke download
config.yaml             Non-secret runtime configuration
Dockerfile              Python 3.12 image with FFmpeg, Playwright, and yutto
docker-compose.yml      bot, web_login, and file_browser services
```

Main flow:

```text
IMAP -> EmailBot -> UrlExtractor -> platform downloader -> SMTP reply
```

## Safety And Boundaries

- Never commit credentials, cookies, yutto auth files, Firefox profiles,
  downloaded media, or logs. Secrets belong in the gitignored `.env`.
- Preserve unrelated user changes in a dirty worktree.
- Do not casually run `test_download.py`: it uses a hardcoded live URL, valid
  credentials, network access, and the configured download destination.
- Network, IMAP, SMTP, browser, and download failures must be logged and handled
  without terminating the long-running poll loop.
- Preserve `_safe_subpath()` checks around every `file_browser.py` route that
  accepts a user path.
- `file_browser.py` is unauthenticated and writable: upload, delete, and
  duplicate-resolution endpoints modify the download tree. Keep it trusted-LAN
  only unless an explicit security change is requested.

## F2 Bootstrap Invariant

`main.py` must patch F2 before importing `email_bot.py` or downloader code. It:

- writes minimal F2/Bark configuration;
- makes Douyin browser-model accessors return dictionaries;
- forces a host-appropriate Firefox/Gecko fingerprint to match the persistent
  Firefox cookies;
- falls back from real to false `msToken` generation;
- tolerates empty Bark configuration.

Moving F2-dependent imports above this bootstrap reproduces import-time crashes
or HTTP 200 responses with empty Douyin data. `test_download.py` duplicates the
patches and must stay synchronized until they move into a shared module.

## Runtime Invariants

### Email processing

- Poll IMAP over SSL for `UNSEEN`, skip the bot's own mail, and deduplicate IMAP
  IDs in memory.
- Apply the optional sender allowlist before cookie commands. Cookie commands
  run before the normal subject-keyword requirement.
- Cooldown is per sender and is set after a successful download.
- Retry cookie-like Douyin failures once after Firefox extraction succeeds.
- Persist transient network/timeout failures in the configured retry queue;
  exhausted links go to the configured failure file.
- Cache successful `v.douyin.com` resolutions so later attempts can use the
  aweme ID without repeating flaky redirects.
- `_safe_logout()` closes the socket directly; do not restore blocking IMAP
  protocol logout after a broken connection.
- Allowlist-, keyword-, and cooldown-skipped mail is initially left unseen but
  already present in `_seen_ids`; the next poll normally marks it seen. A restart
  before then can evaluate it again.

### Douyin downloads

- `DouyinDownloader.download()` is synchronous externally and uses
  `asyncio.run()` internally; metadata comes from F2 and media from `httpx`.
- Regular videos use
  `<root>/<author>/<YYYYMMDD_HHMMSS>_<aweme_id>.mp4` when folderized.
- Static slideshow images go to `<root>/slides/`; animated MP4 clips follow the
  author-folder layout. Extension detection is heuristic and defaults to WebP.
- Downloaded images, regular videos, and animated clips pass through the shared
  `media_processor.py` edge-crop pipeline. Post-processing failures must not
  turn successful downloads into failures.
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
- Keep the strict pixel coverage, per-side crop cap, retained-area floor, and
  90% whole-duration video-frame consensus unless tests justify a safer change.
- Successful crops keep the source as `<stem>_original.bak`. Image writes are
  temporary and atomically replace the destination; all failures restore the
  source.
- H.264 crop dimensions must remain even. Preserve audio by stream copy.
- `process_media.py` is dry-run by default; existing downloads change only with
  explicit `--apply`.

### Cookies

- Cookie acquisition is Firefox-only and uses a persistent Playwright profile.
- First login may be interactive; later extraction reuses the profile headlessly.
- QR generation opens the Douyin login dialog, captures the complete viewport,
  and serializes Firefox access between QR and status requests.
- Keep auth-cookie indicators aligned between `cookie_extractor.py` and
  `douyin_downloader.py`.
- `.env` update helpers currently write in place rather than atomically. Keep
  their formatting consistent and prefer a shared atomic implementation when
  changing them.
- YAML loads `cookie_extractor.headless` and `.validate`, but email-triggered
  extraction currently hardcodes both to `True`.

## Configuration And Paths

For supported overrides, priority is:

```text
environment > config.yaml > dataclass default
```

`config_loader.py` is the source of truth for environment-variable mappings.
Sensitive variables include `EMAIL_ADDRESS`, `EMAIL_PASSWORD`, `DOUYIN_COOKIE`,
and `BILIBILI_AUTH`; `BILIBILI_AUTH_FILE` may reference sensitive login state.
Relative configured paths resolve against the directory containing
`config.yaml`, not the process working directory.

The checked-in config points downloads directly at
`/srv/nas_data/douyin_downloads`. Docker overrides that host path with
`/app/downloads`. `test_download.py` and `migrate_downloads.py` follow the
configured path, while `play.py` ignores `config.yaml` and defaults to the
checkout-local `./downloads/`. Pass the NAS path explicitly when using it on the
deployment host.

The short-link cache defaults to `logs/short_link_cache.json` and can be moved
with `DOUYIN_SHORT_LINK_CACHE`. Cache, retry, failure, media, log, and profile
artifacts must remain untracked.

## Development And Verification

```bash
uv sync
uv run python main.py
uv run python web_login.py
uv run python file_browser.py
uv run python play.py --dry-run --download-dir /srv/nas_data/douyin_downloads
uv run python get_cookie.py
uv run python get_cookie.py --headless
uv run python migrate_downloads.py --dry-run
uv run python process_media.py /srv/nas_data/douyin_downloads
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
sudo docker compose up -d --build bot file_browser
sudo docker compose --profile login up web_login
sudo docker compose down
```

- The bot owns the logs volume; bot and `web_login` share the Firefox-profile
  volume.
- Bot and `file_browser` bind the host NAS root to `/app/downloads`.
- All services bind `config.yaml`; only bot and `web_login` bind `.env`.
- The bot intentionally clears proxy variables so Douyin traffic goes direct.
- The deployment checkout is `~/douyin_email_bot` on `nouveau@nouveauserver`.
  NAS writes outside Docker may require `sudo`.
- When editing elsewhere, push first, then pull on the server and rebuild the
  affected services. When Codex is already in the server checkout, edit and
  verify there, then rebuild as needed.

## Known Gaps

- `README.md` and `.env.example` still contain legacy cookie instructions and
  stale default paths.
- The thumbnail cache is fixed at `/app/.thumb_cache`.
- Flask HTML, CSS, and JavaScript remain inline in Python modules.
- The unused cookie-extractor configuration fields should eventually be
  corrected rather than documented indefinitely.

Any substantial change to architecture, media layout, configuration,
dependencies, or startup/deployment behavior must update this file.
