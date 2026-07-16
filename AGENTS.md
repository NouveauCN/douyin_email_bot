# AGENTS.md - Douyin Email Bot

Repository-level instructions for Codex and other coding agents. This file
applies to the entire repository.

## Project Summary

This is a long-running Python bot and small web-service bundle. The bot polls
an IMAP inbox, extracts Douyin or Bilibili share links from matching emails,
downloads media, and replies over SMTP. The repository also includes:

- Playwright/Firefox cookie acquisition and refresh flows;
- a Flask QR-code login service on port 8080;
- a Flask LAN file browser and playlist UI on port 8081;
- a local random MP4 player;
- Docker Compose deployment for the bot and web services.

Main request flow:

```text
IMAP inbox
  -> EmailBot._poll_once()
  -> EmailBot._process_email()
  -> UrlExtractor
  -> DouyinDownloader or BilibiliDownloader
  -> SMTP reply
```

## Repository Map

```text
main.py                 Bot entry point, F2 bootstrap patches, logging
email_bot.py            IMAP loop, routing, SMTP replies, cookie refresh
douyin_downloader.py    F2 metadata lookup and direct media downloads
bilibili_downloader.py  yutto subprocess wrapper for Bilibili downloads
url_extractor.py        Supported URL regex extraction
config_loader.py        YAML/env configuration dataclasses
cookie_extractor.py     Persistent Playwright Firefox cookie handling
get_cookie.py           Interactive/headless cookie CLI
web_login.py            Flask QR login service
file_browser.py         Flask browser, playlist, upload, dedup, and delete UI
play.py                 Local shuffled playback of downloaded MP4 files
migrate_downloads.py    One-shot slideshow layout migration
test_download.py        Live Douyin integration smoke script
config.yaml             Non-secret application configuration
conf/                   F2 runtime configuration
Dockerfile              Python 3.12-slim image with ffmpeg, Playwright, yutto
docker-compose.yml      bot, web_login, and file_browser services
```

## Non-Negotiable Safety Rules

- Never commit credentials, cookies, browser profiles, downloaded media, or
  logs. Secrets such as `BILIBILI_AUTH` belong in `.env`, which is gitignored;
  yutto auth files must also stay outside Git.
- Preserve unrelated user changes in a dirty worktree.
- Do not run `test_download.py` casually; it uses a hardcoded live Douyin URL,
  requires a valid cookie and network access, and may download media.
- Keep the bot resilient. Expected network, IMAP, SMTP, browser, and download
  failures should be logged and handled without terminating the poll loop.
- Preserve path traversal checks around every route that accepts user-provided
  paths in `file_browser.py`; `_safe_subpath()` is the boundary.

## F2 Bootstrap Rules

`main.py` has order-sensitive bootstrap code. Before importing downloader code,
it writes F2 config and monkey-patches F2 0.0.1.7 behavior:

- Douyin `ClientConfManager.brm_os`, `brm_version`, `brm_browser`, and
  `brm_engine` receive dictionary fallbacks.
- The browser and engine fingerprint is forced to Firefox/Gecko on the host
  platform so it matches cookies acquired through the persistent Firefox
  profile; F2's bundled Edge/Win32 defaults can produce HTTP 200 empty data.
- `TokenManager.gen_real_msToken` falls back to `gen_false_msToken`.
- Bark `ClientConfManager.merge` returns `{}` when both configs are empty.

Do not move F2-dependent imports above this bootstrap. `test_download.py`
duplicates these patches; keep both copies synchronized until they are
deliberately refactored into a shared bootstrap module.

## Runtime Behavior

Email processing in `EmailBot`:

- connects to IMAP over SSL and searches `UNSEEN`;
- skips messages sent by the bot itself;
- deduplicates message IDs in memory for the current process;
- optionally enforces `bot.allowed_senders`;
- routes cookie commands before normal downloads;
- requires `bot.subject_keyword` for normal download requests;
- applies cooldown per sender, not per URL;
- retries Douyin downloads once after cookie-like failures if Firefox cookie
  extraction succeeds;
- queues transient network/timeout download failures for delayed retry before
  sending a final failure; exhausted retry links are appended to the configured
  failed-links file;
- caches successful `v.douyin.com` short-link resolutions to aweme IDs so
  repeated links and manual retries can bypass flaky short-link redirects;
- marks completed and rejected messages as seen; allowlist, keyword, and
  cooldown skips return unseen on their first poll, then the in-process dedup
  path normally marks them seen on the next poll;
- closes IMAP sockets through `_safe_logout()` to avoid protocol logout hangs.

Douyin downloads:

- `DouyinDownloader.download()` is externally synchronous and uses
  `asyncio.run()` internally.
- F2 resolves the share URL and fetches metadata; `httpx` performs direct media
  downloads.
- Regular videos save as `downloads/<author>/<YYYYMMDD_HHMMSS>_<aweme_id>.mp4`
  using download time when `folderize` is enabled.
- Static slideshow images save under `downloads/slides/`.
- Slideshow filenames use the same download-time prefix. Animated clips save in
  the same author-folder layout as videos.
- Downloaded MP4 files pass through `_auto_crop_video()`. Cropping must restore
  the original if ffmpeg fails.

Bilibili downloads:

- `BilibiliDownloader.download()` shells out to the yutto CLI. Keep yutto as a
  CLI integration unless there is an explicit reason to depend on internals.
- Bilibili's dataclass default is `downloads/bilibili/` (the checked-in YAML
  and Docker overrides currently point at the NAS), mp4 format, highest
  configured video quality, codec preference `hevc,avc,av1`, no danmaku,
  subtitles, progress, or color. yutto's current default does not retain a
  standalone audio sidecar.
- yutto may still download audio needed to mux the final video with sound.
- Cover sidecars are allowed, but are moved to `downloads/slides/` with a
  `bilibili_` prefix and must not count as video download results.
- A single Bilibili URL may produce multiple media files. Preserve
  `files`/`file_count` metadata and keep email replies useful for both single
  and multi-file outcomes.
- yutto requires Python 3.11+ and FFmpeg. Keep it out of the bot's main Python
  dependency set because yutto and F2 currently require incompatible
  `aiofiles`/`pydantic` versions. Docker installs yutto in `/opt/yutto` and
  exposes only the `yutto` executable.

Cookie handling:

- Cookie extraction is Firefox-only and uses a persistent Playwright profile.
- First login can be interactive through `get_cookie.py`; later headless
  extraction reuses the profile.
- The web QR login service opens the Douyin login dialog before capturing the
  complete viewport and serializes Firefox access between QR/status requests.
- Cookie auth indicators in `cookie_extractor.py` and `douyin_downloader.py`
  should stay logically consistent.
- `.env` update logic exists in multiple modules. The implementations currently
  preserve similar formatting but write in place rather than atomically; keep
  them consistent and prefer a shared atomic implementation when changing them.

## Configuration

For fields that have an environment-variable override, configuration priority
is:

```text
environment variables > config.yaml > dataclass defaults
```

Secret values belong in `.env`:

- `EMAIL_ADDRESS`
- `EMAIL_PASSWORD`
- `DOUYIN_COOKIE`
- `BILIBILI_AUTH`

`BILIBILI_AUTH_FILE` may point to a yutto authentication file containing
sensitive login state; do not commit that file.

Important runtime overrides:

| Environment variable | Configuration field |
|---|---|
| `DOUYIN_DOWNLOAD_PATH` | `douyin.download_path` |
| `DOUYIN_TIMEOUT` | `douyin.timeout` |
| `DOUYIN_MAX_RETRIES` | `douyin.max_retries` |
| `DOUYIN_MAX_TASKS` | `douyin.max_tasks` |
| `DOUYIN_SHORT_LINK_CACHE` | short-link cache file path |
| `BILIBILI_DOWNLOAD_PATH` | `bilibili.download_path` |
| `BILIBILI_AUTH` | `bilibili.auth` |
| `BILIBILI_AUTH_FILE` | `bilibili.auth_file` |
| `BILIBILI_TIMEOUT` | `bilibili.timeout` |
| `BILIBILI_BATCH` | `bilibili.batch` |
| `BILIBILI_VIDEO_QUALITY` | `bilibili.video_quality` |
| `BILIBILI_YUTTO_BIN` | `bilibili.yutto_bin` |
| `EMAIL_POLL_INTERVAL` | `email.poll_interval` |
| `BOT_ALLOWED_SENDERS` | `bot.allowed_senders` |
| `BOT_COOLDOWN_SECONDS` | `bot.cooldown_seconds` |
| `BOT_SUBJECT_KEYWORD` | `bot.subject_keyword` |
| `BOT_TRANSIENT_RETRY_ATTEMPTS` | `bot.transient_retry_attempts` |
| `BOT_TRANSIENT_RETRY_DELAY_SECONDS` | `bot.transient_retry_delay_seconds` |
| `COOKIE_PROFILE_DIR` | `cookie_extractor.profile_dir` |
| `ENV_AUTO_RELOAD` | enables `.env` cookie hot reload |

Download paths are resolved against the directory containing `config.yaml`, so
effective paths do not depend on the process working directory.

## Development Commands

Install dependencies:

```bash
uv sync
```

`file_browser.py` imports Pillow. Docker installs it from `requirements.txt`,
but it is not currently declared in `pyproject.toml`/`uv.lock`; a local
`uv sync` environment needs Pillow installed separately before running the file
browser.

Run locally:

```bash
uv run python main.py
uv run python web_login.py
uv run python file_browser.py
uv run python play.py --dry-run --download-dir /srv/nas_data/douyin_downloads
```

Cookie utilities:

```bash
uv run python get_cookie.py
uv run python get_cookie.py --headless
uv run python get_cookie.py --no-validate
uv run python get_cookie.py --profile PATH
```

Preview slideshow layout migration:

```bash
uv run python migrate_downloads.py --dry-run
```

Verification:

```bash
git diff --check
uv run python -m compileall .
```

For documentation-only changes, `git diff --check` is sufficient. For Python
changes, compile affected modules at minimum and add focused tests where
practical. Mock IMAP, SMTP, browser, network, and filesystem side effects.

## Docker And Deployment

Compose services:

- `bot`: long-running email bot;
- `file_browser`: long-running LAN download browser;
- `web_login`: on-demand QR login service behind the `login` profile.

Typical service commands:

```bash
sudo docker compose up -d --build bot file_browser
sudo docker compose --profile login up web_login
sudo docker compose down
```

The bot owns the named logs volume, while the bot and `web_login` share the
Firefox profile volume. Downloads are bind-mounted into the bot and
`file_browser` from `/srv/nas_data/douyin_downloads` on the documented Docker
host (→ `/app/downloads` inside containers). All services bind-mount
`config.yaml`; only the bot and `web_login` bind-mount `.env`.
The `bot` service intentionally clears proxy environment variables in Compose;
Douyin requests should go out directly even if the Docker host has proxy
settings configured.

**Important:** The checked-in `config.yaml` currently points Douyin and
Bilibili downloads directly at `/srv/nas_data/douyin_downloads`, so
`test_download.py` and `migrate_downloads.py` operate on the NAS path when run
on the deployment host. `play.py` is the exception: it defaults to the
checkout's `./downloads/` and needs `--download-dir
/srv/nas_data/douyin_downloads` to play deployed media. The NAS root is
currently owned by `197609:1000` with mode `755`, so use `sudo` when creating
author directories or otherwise writing outside the Docker services:

```bash
sudo mkdir -p /srv/nas_data/douyin_downloads/新作者名
```

The documented execution host is `nouveau@nouveauserver` (currently
`192.168.1.94`), with the checkout at `~/douyin_email_bot/`. Treat this as the
deployment default, not a portable code assumption.

Codex usually works directly in the remote Linux checkout, so it may edit and
verify in `~/douyin_email_bot/` and restart services there. Claude Code on a
Windows development host cannot run this deployment directly; in that case,
after making changes:

1. Push all code changes to GitHub.
2. SSH to the server:
   ```bash
   ssh nouveau@nouveauserver
   ```
3. Sync and restart from the server checkout:
   ```bash
   cd ~/douyin_email_bot
   git pull
   sudo docker compose up -d --build bot file_browser
   ```
4. If QR login is needed, start it explicitly:
   ```bash
   sudo docker compose --profile login up web_login
   ```

For Windows/Claude Code development, code changes should be made locally,
reviewed, pushed through GitHub, pulled on the server, and then the services
must be rebuilt/restarted. For Codex already running on the remote Linux host,
work directly in the server checkout and restart services after verification.

## Known Pitfalls

- `README.md` and `.env.example` contain legacy cookie instructions and stale
  default download paths. Prefer the current implementation and this file when
  they conflict, and update user-facing docs when behavior changes.
- Slideshow extension detection is heuristic and defaults to `.webp`.
- `douyin.max_tasks` exists in config, but the single-download handler currently
  forces `max_tasks=1`.
- Allowlist-, keyword-, and cooldown-skipped messages are left unseen on the
  first poll, but their IMAP IDs have already entered `_seen_ids`; the next poll
  normally marks them seen. A process restart before that next poll can cause
  them to be evaluated again.
- `_safe_logout()` and the 30-second IMAP socket timeout prevent hangs after
  stale or severed connections.
- `file_browser.py` is an unauthenticated writable service: upload, delete, and
  duplicate-resolution endpoints can modify the download tree. Treat the whole
  service as trusted-LAN-only unless an explicit security change is requested.
- The thumbnail cache is fixed at `/app/.thumb_cache`.
- `cookie_extractor.headless` and `cookie_extractor.validate` are loaded from
  YAML, but email-triggered extraction currently hardcodes both to `True`.
- `play.py` does not load `config.yaml`; its default directory remains the
  checkout-local `./downloads/`.
- Pillow is present in `requirements.txt` for Docker but absent from
  `pyproject.toml`/`uv.lock`, so `uv sync` alone does not prepare the local file
  browser environment.
- Flask templates, CSS, and JavaScript are inline in Python modules; avoid broad
  rewrites unless the task requires them.

Any substantial change that adds a module, changes architecture or media
layout, modifies configuration or dependencies, or alters startup/deployment
behavior must update this `AGENTS.md` in the same change.
