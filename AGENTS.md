# AGENTS.md — Douyin Email Bot

This file contains repository-level instructions for Codex and other coding
agents. It applies to the entire repository.

## Project overview

This is a long-running Python email bot that:

1. Polls an IMAP inbox for unread messages.
2. Extracts Douyin or Bilibili share links from matching emails.
3. Downloads videos or slideshow media.
4. Replies to the sender over SMTP.

The repository also includes:

- a Playwright/Firefox cookie acquisition flow;
- a Flask QR-code login service;
- a Flask LAN file browser and playlist UI;
- a local random video player;
- Docker Compose deployment for the bot and web services.

## Repository map

```text
.
├── main.py                 # Bot entry point, F2 bootstrap patches, logging
├── email_bot.py            # IMAP loop, routing, SMTP replies, cookie refresh
├── douyin_downloader.py    # F2 metadata lookup and direct media downloads
├── bilibili_downloader.py  # yutto subprocess wrapper for Bilibili downloads
├── url_extractor.py        # Supported URL regex extraction
├── config_loader.py        # YAML/env configuration dataclasses
├── cookie_extractor.py     # Persistent Playwright Firefox cookie handling
├── get_cookie.py           # Interactive/headless cookie CLI
├── web_login.py            # Flask QR login service on port 8080
├── file_browser.py         # Flask download browser on port 8081
├── play.py                 # Local shuffled playback of downloaded MP4 files
├── migrate_downloads.py    # One-shot slideshow layout migration
├── test_download.py        # One-shot integration smoke test
├── config.yaml             # Non-secret application configuration
├── conf/                   # F2 runtime configuration
├── Dockerfile
└── docker-compose.yml
```

## Architecture and main flows

```text
IMAP inbox
  -> EmailBot._poll_once()
  -> EmailBot._process_email()
  -> UrlExtractor
  -> DouyinDownloader or BilibiliDownloader
  -> SMTP reply
```

Cookie refresh may enter the flow from email commands, `.env` hot reload, the
Playwright Firefox profile, or the web QR login service.

### Bot startup

`main.py` has order-sensitive bootstrap code. Before importing downloader code,
it writes F2 config and monkey-patches F2 internals to work around behavior in
F2 0.0.1.7:

- `ClientConfManager.brm_os`, `brm_version`, `brm_browser`, and `brm_engine`
  receive dictionary fallbacks.
- `TokenManager.gen_real_msToken` falls back to `gen_false_msToken`.
- Bark's `ClientConfManager.merge` returns an empty dict when both configs are
  empty.

Do not move F2-dependent imports above this bootstrap. `test_download.py`
duplicates the same patches; keep both copies synchronized until they are
deliberately refactored into a shared bootstrap module.

After bootstrap, startup loads `.env`, reads `config.yaml`, validates required
email credentials, assesses cookie quality, configures rotating logs, and
starts the blocking email polling loop.

### Email processing

`EmailBot`:

- connects to IMAP over SSL and searches `UNSEEN`;
- skips messages sent by the bot itself;
- keeps an in-memory message-ID dedup set;
- optionally enforces `bot.allowed_senders`;
- routes cookie update commands before normal downloads;
- requires `bot.subject_keyword` for normal download requests;
- applies a per-sender cooldown;
- retries once after a cookie-related failure if Firefox extraction succeeds;
- marks handled messages as seen;
- closes the IMAP socket directly through `_safe_logout()` to avoid blocking
  on protocol logout after broken connections.

The poll loop must remain resilient: expected network, IMAP, and SMTP failures
should be logged per cycle without terminating the service.

### Downloads

`DouyinDownloader.download()` is synchronous externally and uses
`asyncio.run()` internally. F2 resolves the share URL and fetches metadata;
`httpx` downloads the selected media.

Current output layout:

- regular videos: `downloads/<author>/<YYYYMMDD>_<aweme_id>.mp4` when
  `folderize` is enabled;
- static slideshow images:
  `downloads/slides/<YYYYMMDD>_<aweme_id>_<NN>.<ext>`;
- slideshow animated clips: the normal author folder, using the same date and
  aweme ID prefix as regular videos.

Downloaded MP4 files are passed through `_auto_crop_video()`. If `ffmpeg`
detects substantial, consistent black bars, the original is renamed to
`*_original.bak` and a cropped H.264 file replaces it. Failure must restore the
original.

### Bilibili downloads

`BilibiliDownloader.download()` shells out to the yutto CLI executable.
Keep this as a CLI integration unless there is a deliberate reason to depend on
yutto internals.

Current defaults:

- output directory: `downloads/bilibili/`;
- output format: mp4;
- one selected video stream per video, requesting the highest available quality
  by default (`bilibili.video_quality` / `BILIBILI_VIDEO_QUALITY` default 127);
- preferred video codec order: HEVC, AVC, AV1;
- no danmaku, subtitles, or standalone audio sidecar files;
- audio streams may still be downloaded when yutto needs them to mux a final
  video with sound;
- cover image sidecars are allowed, but move them into `downloads/slides/` with
  a `bilibili_` prefix and do not count them as video download results;
- no progress and no color for cleaner logs;
- optional auth via `BILIBILI_AUTH="SESSDATA=...; bili_jct=..."`;
- optional yutto auth file via `bilibili.auth_file` / `BILIBILI_AUTH_FILE`;
- optional batch mode via `bilibili.batch` / `BILIBILI_BATCH`.
- optional CLI path via `bilibili.yutto_bin` / `BILIBILI_YUTTO_BIN`.

yutto requires Python 3.11+ and FFmpeg. Bilibili downloads do not use the
Douyin cookie refresh flow. yutto must stay out of the bot's main Python
dependency set because yutto and F2 currently require incompatible versions of
`aiofiles`/`pydantic`; Docker installs yutto in `/opt/yutto` and exposes only
the `yutto` executable.

A single Bilibili URL may produce multiple media files, such as multi-part
submissions, collections, or batch bangumi/series downloads. Preserve the
`files`/`file_count` result metadata and keep email replies useful for both
single-file and multi-file outcomes.

### Cookie handling

Cookie extraction is Firefox-only and uses a persistent Playwright profile.
The first login can be performed interactively with `get_cookie.py`; later
headless extraction reuses that profile.

Relevant entry points:

```bash
uv run python get_cookie.py
uv run python get_cookie.py --headless
uv run python get_cookie.py --no-validate
uv run python get_cookie.py --profile PATH
```

`web_login.py` exposes:

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | QR login UI |
| `/api/qr` | GET | Capture the login QR image |
| `/api/status` | GET | Check auth cookies and persist them |
| `/api/stop` | POST | Return a shutdown acknowledgement |

`/api/stop` currently does not terminate the Flask process.

### File browser and players

`file_browser.py` serves the configured download directory and includes raw
file serving, video thumbnails, individual playback, a browser playlist,
image display, and deletion through `/api/delete`.

Important details:

- `_safe_subpath()` is the path traversal boundary. Preserve equivalent checks
  for every route that accepts a path.
- `/api/delete` is destructive and the service has no authentication. Treat it
  as a trusted-LAN service unless an explicit security change is requested.
- thumbnail generation requires `ffmpeg`;
- the thumbnail cache is fixed at `/app/.thumb_cache`;
- Flask templates, CSS, and JavaScript are inline in the Python modules.

`play.py` recursively discovers MP4 files, creates a fresh random shuffle on
each invocation, supports mpv/VLC playlist mode, and otherwise launches files
sequentially with background page-cache preloading.

## Configuration contract

Configuration priority is:

```text
environment variables > config.yaml > dataclass defaults
```

Secret values belong in `.env`, which is gitignored:

- `EMAIL_ADDRESS`
- `EMAIL_PASSWORD`
- `DOUYIN_COOKIE`

Never commit actual credentials, cookies, browser profiles, downloaded media,
or logs.

Supported runtime overrides include:

| Environment variable | Configuration field |
|---|---|
| `DOUYIN_DOWNLOAD_PATH` | `douyin.download_path` |
| `DOUYIN_TIMEOUT` | `douyin.timeout` |
| `DOUYIN_MAX_RETRIES` | `douyin.max_retries` |
| `DOUYIN_MAX_TASKS` | `douyin.max_tasks` |
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
| `COOKIE_PROFILE_DIR` | `cookie_extractor.profile_dir` |
| `ENV_AUTO_RELOAD` | enable `.env` cookie hot reload |

Download paths are resolved against the directory containing `config.yaml`, so
effective paths do not depend on the process working directory.

## Development commands

Install dependencies:

```bash
uv sync
```

Run the bot:

```bash
uv run python main.py
```

Run auxiliary services:

```bash
uv run python web_login.py
uv run python file_browser.py
uv run python play.py --dry-run
```

Preview the one-shot layout migration:

```bash
uv run python migrate_downloads.py --dry-run
```

There is currently no hermetic automated test suite. `test_download.py` is an
integration script with a hardcoded live Douyin URL. It requires a valid cookie
and network access and may download media. Do not run it casually as part of
routine verification.

For documentation-only changes, verify at minimum:

```bash
git diff --check
```

For Python changes, also compile affected modules:

```bash
uv run python -m compileall .
```

Add focused tests where practical. Mock IMAP, SMTP, browser, network, and
filesystem side effects in unit tests; do not make routine tests depend on
live Douyin or email services.

## Docker and deployment

The Docker image uses `python:3.12-slim`, installs `ffmpeg`, installs the main
Python requirements, installs yutto into an isolated `/opt/yutto` virtual
environment, then installs Playwright Firefox. Keep Python compatibility in
mind when changing the base image because F2's dependency chain includes
compiled packages.

Compose services:

- `bot`: long-running email bot;
- `web_login`: on-demand service behind the `login` profile;
- `file_browser`: long-running LAN download browser.

Typical local/server commands:

```bash
sudo docker compose up -d --build bot file_browser
sudo docker compose --profile login up web_login
sudo docker compose down
```

The services share named volumes for downloads, logs, and the Firefox profile.
The `.env` file and `config.yaml` are bind-mounted. The Docker host currently
documented by the project is `nouveau@192.168.0.103`, with the checkout at
`~/douyin_email_bot/`; treat these as deployment defaults, not portable code
assumptions.

The remote host is execution-only. Code changes should be made in a development
checkout, reviewed and pushed through Git, then pulled on the server.


## Change guidelines and known pitfalls

- Preserve the F2 bootstrap import order.
- Keep duplicated F2 patches in `main.py` and `test_download.py` synchronized.
- Keep cookie auth indicators in `cookie_extractor.py` and
  `douyin_downloader.py` logically consistent.
- Slideshow extension detection is heuristic and defaults to `.webp`.
- The downloader's `max_tasks` configuration is passed into its dataclass, but
  the single-download handler currently forces `max_tasks=1`.
- Cooldown is per sender, not per URL.
- The message dedup set is in memory only and resets on restart.
- If an unallowed or cooldown-limited message is left unseen, it may be found
  again on later polls; understand the existing behavior before changing mail
  flag handling.
- `_safe_logout()` and the 30-second IMAP socket timeout prevent hangs after
  stale or severed connections.
- `.env` update logic exists in multiple modules. Keep behavior consistent if
  changing its formatting or atomicity.
- `README.md` contains some legacy cookie instructions. Prefer the current
  implementation and this file when they conflict, and update user-facing docs
  when behavior changes.
- Avoid broad rewrites of the large inline Flask templates unless the task
  requires them.
- Preserve unrelated user changes in a dirty worktree.

Any substantial change that adds a module, changes architecture or media
layout, modifies configuration or dependencies, or alters startup/deployment
behavior must update this `AGENTS.md` in the same change.
