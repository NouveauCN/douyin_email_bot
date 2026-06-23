# CLAUDE.md — Douyin Email Bot

Email bot that monitors an IMAP inbox for Douyin share links, downloads videos/slideshows, and replies via SMTP. Runs as a long-lived polling loop.

## Project structure

```
douyin_email_bot/
├── main.py                 # Entry point — F2 monkey-patches, logging setup, bot startup
├── email_bot.py            # IMAP poll loop, email parsing, command routing, cookie refresh
├── douyin_downloader.py    # Video & slideshow downloader (wraps F2 async API → sync)
├── url_extractor.py        # Regex-based Douyin URL extraction from text
├── cookie_extractor.py     # Playwright + headless Firefox cookie extraction & validation
├── get_cookie.py           # CLI: interactive/headless cookie acquisition → write .env
├── config_loader.py        # YAML + env-var config via dataclasses (AppConfig)
├── play.py                 # Random video player — shuffle + no-repeat across runs
├── migrate_downloads.py    # One-shot: move *_slides/ dirs to downloads/slides/
├── test_download.py        # One-shot download test (duplicates main.py's F2 patches)
├── config.yaml             # Non-sensitive settings (email server, bot behavior, etc.)
├── .env.example            # Template for secrets (EMAIL_ADDRESS, EMAIL_PASSWORD, DOUYIN_COOKIE)
├── pyproject.toml          # uv project — deps: f2, playwright, python-dotenv, pyyaml
├── requirements.txt        # pip deps (for Docker builds)
├── Dockerfile              # Ubuntu 26.04 + Playwright Firefox container
├── docker-compose.yml      # bot + web_login services
├── .dockerignore           # exclude non-Docker files from build context
├── web_login.py            # Flask web service — QR login on port 8080
├── conf/
│   ├── conf.yaml           # F2 runtime config — Bark disabled
│   └── app.yaml            # F2 app config — empty Bark block
└── scripts/
    ├── setup_task.ps1       # Register bot as a hidden Windows scheduled task (Admin)
    ├── teardown_task.ps1    # Stop and remove the scheduled task (Admin)
    └── launcher.ps1         # Task Scheduler entry point — finds uv.exe, starts bot
```

## Architecture

```
Email (IMAP) → EmailBot._poll_once() → UrlExtractor → DouyinDownloader → SMTP reply
                                         ↑
                                   cookie_extractor (auto-refresh on failure)
```

### Background service (Windows Task Scheduler)

The bot can run as a pure background task with zero terminal visibility, auto-starting on system boot.

**Setup** (Run as Administrator):
```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_task.ps1
```

**How it works:**
- `setup_task.ps1` registers a Task Scheduler task named `DouyinEmailBot`
- Trigger: at system startup (+ up to 60s random delay)
- `launcher.ps1` is the entry point — locates `uv.exe`, sets project CWD, runs the bot
- The task runs `powershell.exe -WindowStyle Hidden` — no console window ever appears
- Auto-restart: 5 retries at 1-minute intervals on crash
- Logon options: `Interactive` (no password, runs when logged in) or `-Password` (runs before login, prompts for credentials)

**Teardown** (Run as Administrator):
```powershell
powershell -ExecutionPolicy Bypass -File scripts\teardown_task.ps1
```

**Logging in background mode:**
- `setup_logging()` in `main.py` writes to `logs/bot.log` (10 MB rotating, 5 backups) at DEBUG level
- Console handler is only attached when `sys.stdout.isatty()` — i.e., not in background mode
- ANSI escape codes from `colorama` are stripped from file output via `_AnsiStrippingFormatter`

**Manual commands:**
```powershell
Start-ScheduledTask -TaskName "DouyinEmailBot"    # Start immediately
Stop-ScheduledTask -TaskName "DouyinEmailBot"     # Stop the bot
Get-ScheduledTask -TaskName "DouyinEmailBot" | Format-List State, LastRunTime, LastTaskResult
Get-Content logs\bot.log -Tail 50                  # View recent logs
```

### Docker deployment (Ubuntu 26 LTS)

Multi-service Docker Compose setup: a persistent bot + an on-demand web QR login service.

**Build & start the bot:**
```bash
docker compose up -d bot
```

**QR login (when you need to re-authenticate with Douyin):**
```bash
docker compose --profile login up web_login
# Open http://<host>:8080 → scan QR with Douyin app → cookie auto-saved to .env
# Ctrl+C when done, then restart bot (or rely on ENV_AUTO_RELOAD=1)
```

**Teardown:**
```bash
docker compose down           # stop bot
docker compose down -v        # also delete volumes (downloads, logs, profile)
```

**Architecture:**
- `bot` service: `restart: unless-stopped`, long-lived poll loop
- `web_login` service: `profiles: [login]`, on-demand Flask app on port 8080
- Shared `firefox_profile` volume so web login + bot share persistent login state
- `ENV_AUTO_RELOAD=1` enables `.env` mtime watching — bot picks up new cookie without restart

**Docker env-var overrides** (all optional, with YAML fallbacks):
| Env Var | Config Field |
|---|---|
| `DOUYIN_DOWNLOAD_PATH` | `douyin.download_path` |
| `DOUYIN_TIMEOUT` | `douyin.timeout` |
| `DOUYIN_MAX_RETRIES` | `douyin.max_retries` |
| `EMAIL_POLL_INTERVAL` | `email.poll_interval` |
| `BOT_ALLOWED_SENDERS` | `bot.allowed_senders` (comma-separated) |
| `BOT_COOLDOWN_SECONDS` | `bot.cooldown_seconds` |
| `COOKIE_PROFILE_DIR` | `cookie_extractor.profile_dir` |
| `ENV_AUTO_RELOAD` | Enable `.env` mtime hot-reload (`1`/`true`/`yes`) |

**Web login API** (`web_login.py`):
| Route | Method | Purpose |
|---|---|---|
| `/` | GET | QR scanner UI (single-page HTML) |
| `/api/qr` | GET | Launch headless Firefox, screenshot QR → `{qr_image, success}` |
| `/api/status` | GET | Poll auth cookies → `{status, cookie_str, auth_count}` |
| `/api/stop` | POST | Graceful shutdown |

### Entry point (`main.py`)

1. **Bootstrap order matters.** Before any F2 import, writes `conf/{conf,app}.yaml` to disable Bark (avoids 405 errors). Then monkey-patches three F2 internals:
   - `ClientConfManager.brm_os/version/browser/engine` — F2 returns `str` instead of `dict` when config is missing, crashing pydantic. Patch forces dict fallbacks.
   - `TokenManager.gen_real_msToken` — F2 0.0.1.7 bug: exception handler calls `gen_real_msToken()` again instead of `gen_false_msToken()`. Patch catches and falls back.
   - `ClientConfManager.merge` (bark) — `ValueError` when both bark configs are empty. Patch returns `{}`.
2. Loads `.env` via `python-dotenv`, then `config.yaml` via `config_loader.load_config()`.
3. Assesses cookie quality on startup (logged-in vs anonymous vs minimal).
4. Creates `EmailBot` and calls `.run()` (blocking poll loop).

### Email bot (`email_bot.py`)

`EmailBot` class — single `run()` method with infinite poll loop.

**Poll cycle** (`_poll_once`):
- IMAP connect → SELECT INBOX → SEARCH UNSEEN → fetch each → process
- Connection errors (IMAP, SMTP, network) are caught per cycle; unexpected exceptions are logged and the loop continues

**Per-email processing** (`_process_email`):
1. Skip own replies (`sender == cfg.email`) — prevents infinite loop
2. Dedup via `_seen_ids` set — prevents processing same message twice
3. Sender allowlist check (`bot.allowed_senders`)
4. **Command routing** — subject keyword match (configurable):
   - `commands.cookie_update` (default "更新cookie") → `_handle_cookie_update()` — reads cookie from email body, writes `.env`, hot-reloads
   - `commands.cookie_auto` (default "自动获取cookie") → `_handle_cookie_auto()` — headless Playwright extraction
5. **Download routing** — subject must contain `bot.subject_keyword` (default "下载"):
   - Extract URL via `UrlExtractor.extract(subject + body)`
   - Cooldown check per sender
   - Call `DouyinDownloader.download(url)`
   - **Auto cookie refresh on failure**: if error message contains cookie-related keywords (删/私密/cookie/异常), attempts headless Playwright extraction from Firefox profile, hot-reloads cookie, retries once
6. Mark email as seen (tries `\Seen` then `Seen` for compatibility)

**Helpers**: `_extract_addr`, `_decode_str`, `_get_body_text`, `_mark_seen`, `_write_env`.

### URL extractor (`url_extractor.py`)

Single regex pattern matching:
- `https://v.douyin.com/<short_id>` (share links)
- `https://www.douyin.com/video/<id>` or `.../note/<id>` (full URLs)

`UrlExtractor.extract(text)` → first match or `None`.

### Douyin downloader (`douyin_downloader.py`)

`DouyinDownloader` bridges F2's async API into synchronous `download(url) → dict`.

**Flow**:
1. Cookie sanity check (length > 500 or has auth token names)
2. `asyncio.run(_download_async())`
3. F2 `DouyinHandler` + `AwemeIdFetcher` resolve short link → aweme_id → metadata
4. **Slideshow path** (`media_type=42` / `aweme_type=68`): images present but no `video_play_addr` → `_download_slideshow()`
   - Downloads BOTH static `.webp`/`.jpg` images AND animated `.mp4` clips (`images_video`)
   - Saved to `slides/{date}_{aweme_id}_slides/` directory
5. **Video path**: first `video_play_addr` → direct httpx download
6. **Error diagnostics**: when no playable content, inspects `api_status_code`, `is_delete`, `is_prohibited`, `private_status` to build Chinese error messages

**File naming**: `{YYYYMMDD}_{aweme_id}.mp4` under `{author_name}/` (video) or `slides/{date}_{aweme_id}_slides/` (slideshow, always flat under `slides/`).

**httpx download**: up to 3 retries with 1s delay, custom User-Agent + Referer headers.

### Cookie extractor (`cookie_extractor.py`)

Firefox-only persistent profile extraction via Playwright.

**Key design**: persistent `user_data_dir` so login state survives across runs. First use requires interactive login (`get_cookie.py` without `--headless`); subsequent headless runs reuse saved cookies.

**Entry point**: `extract_cookies(profile_dir, headless, validate)` → `(cookie_str | None, status_msg)`

**Quality assessment** (`_assess_quality`):
- ≥3 auth cookies (sessionid, passport_csrf_token, odin_tt, uid, etc.) → "已登录"
- ≥1 auth cookie → "已登录"
- ≥10 total cookies → "匿名会话"
- otherwise → "基础会话"

**Validation** (`validate_cookie`): httpx GET to douyin.com with the cookie, checks for login redirect or 401/403.

### CLI cookie tool (`get_cookie.py`)

```
uv run python get_cookie.py              # interactive (visible Firefox, scan QR)
uv run python get_cookie.py --headless   # extract from persistent profile
uv run python get_cookie.py --no-validate
uv run python get_cookie.py --profile PATH
```

Interactive mode: launches VISIBLE Firefox, user scans QR code to login, presses Enter to extract.

### Config loader (`config_loader.py`)

Five dataclasses: `EmailConfig`, `DouyinConfig`, `BotCommands`, `BotConfig`, `CookieExtractorConfig` → `AppConfig`.

Priority: env vars > YAML values > dataclass defaults. Secrets (email, password, cookie) are env-var gated.

### Random player (`play.py`)

Plays ALL downloaded `.mp4` files in one run with a fresh random shuffle each invocation. No state file — every run is independent.

```
uv run python play.py                  # Play all videos in random order
uv run python play.py --dry-run        # Show queue without playing
uv run python play.py --player mpv     # Use specific player (auto-detects mpv)
uv run python play.py --preload 3      # Preload N upcoming videos (default 3)
uv run python play.py --ignore PATH    # Skip specific video (repeatable)
```

**Design**:
- **Fresh seed every run**: `os.urandom(8)` → 64-bit random seed. Every invocation produces a different shuffle. Seed is printed for reproducibility.
- **All-at-once playback**: single run plays through the entire shuffled queue sequentially.
- **Lazy preloading**: `VideoPreloader` background thread reads upcoming 2–3 video files to warm the OS page cache, eliminating cold-cache stutter. Configurable via `--preload`.
- **Player auto-detection**: ① Playlist-capable players (mpv, VLC) → writes temp `.m3u`, one process plays all videos with native transitions. ② Sequential-only players (MPC-HC, PotPlayer, WMP) → one-by-one with preloader. ③ Windows `assoc`/`ftype` registry lookup. ④ OS shell fallback. Detection order: known install paths → PATH → assoc/ftype → system default.
- **Mpv playlist mode**: if `mpv` is on PATH, uses `mpv --playlist` with a temp `.m3u` file for seamless transitions with native pre-buffering.
- **Skips images**: only collects `.mp4` files (slideshow `.webp`/`.jpg` files are naturally excluded by the `.mp4` glob).
- **Ctrl+C behavior**: first interrupt skips current video and moves to next; second interrupt within the same playback exits.
- **Video discovery**: `download_dir.rglob("*")` filtered to `suffix == ".mp4"`, sorted before shuffle for deterministic ordering pre-randomization.

### Test script (`test_download.py`)

One-shot download of a hardcoded URL. Duplicates main.py's F2 monkey-patches (bootstrap must happen before F2 imports). Useful for quick validation.

## Key dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `f2` | ≥0.0.1.7 | Douyin API client (metadata + download orchestration) |
| `playwright` | ≥1.60.0 | Headless Firefox for cookie extraction |
| `python-dotenv` | ≥1.0.0 | `.env` file loading |
| `pyyaml` | ≥6.0 | YAML config parsing |
| `httpx` | (transitive) | Direct file downloads |
| `colorama` | (transitive) | Windows console color |

## Config file contract

- `config.yaml` — non-sensitive settings (servers, ports, timeouts, keywords). Safe to commit.
- `.env` — secrets (`EMAIL_ADDRESS`, `EMAIL_PASSWORD`, `DOUYIN_COOKIE`). Gitignored.
- `conf/{conf,app}.yaml` — F2 runtime config, auto-generated by bootstrap code. Not user-editable.

## Known quirks & pitfalls

1. **F2 monkey-patching is fragile.** The three patches in `main.py` target specific F2 0.0.1.7 bugs. Upgrading F2 may break or obsolete these patches. The `test_download.py` file duplicates the same patches — if you change one, change the other. The fallback values are platform-aware (detects `sys.platform` to send `"Linux"`/`"Windows"`/`"Darwin"` as OS name to Douyin's API).

2. **Firefox-only cookie extraction.** `cookie_extractor.py` only supports Firefox (Playwright `p.firefox.launch_persistent_context`). Chrome/Edge would need different API (CDP-based extraction).

3. **Cookie auth indicators** are checked in TWO places with slightly different logic: `cookie_extractor._assess_quality()` uses `_AUTH_COOKIE_NAMES` frozenset; `douyin_downloader.download()` checks a hardcoded list `["sessionid", "passport_csrf_token", "odin_tt", "uid"]`. These should stay in sync.

4. **Slideshow file extension detection** is heuristic: checks the first static image URL for `.jpg`/`.jpeg`/`.png` substring, defaults to `.webp`. Non-standard URLs without extensions in path will get `.webp`.

5. **IMAP `\Seen` flag** tries two forms (`\\Seen` and `Seen`) because some servers reject the backslash form. If marking fails, the message will be re-processed on next poll (dedup via `_seen_ids` protects against this).

6. **Cooldown is per-sender, not per-URL.** Sending two different links within the cooldown window will skip the second.

7. **Logging is dual-output.** `setup_logging()` now writes DEBUG-level logs to `logs/bot.log` (RotatingFileHandler, 10 MB × 5 backups) with ANSI codes stripped. Console output (INFO level, with colors) is only attached when `sys.stdout.isatty()` — i.e., not when running as a background scheduled task. All modules use named loggers (`logging.getLogger("ModuleName")`) which inherit from the root config.

8. **`download_path` is resolved to absolute in `config_loader.py`.** The `"./downloads"` default is resolved against the `config.yaml` directory at load time so downloads always land in the project tree regardless of CWD. No change needed in `config.yaml`.

9. **`_safe_logout` replaces `mail.logout()` in `finally`.** After a network error (SSL EOF, timeout), the TCP connection is already broken. Calling `mail.logout()` would send the IMAP `LOGOUT` command and then block in `recv()` waiting for a server response that never arrives — freezing the entire bot inside the `finally` block. `_safe_logout()` instead calls `sock.shutdown(SHUT_RDWR)` + `sock.close()` at the TCP level, which never blocks. `_imap_connect()` also sets a 30s socket timeout (`mail.socket().settimeout(30)`) so future operations on a stale connection fail fast rather than hang.

## Network error handling

The poll loop catches `ConnectionError`, `OSError`, `imaplib.IMAP4.error`, `smtplib.SMTPException` per cycle and retries after `poll_interval` seconds. The errors reported in logs (WinError 10053/10060, SSL EOF) are expected behaviors from:
- WeChat/Douyin server rate-limiting or anti-scraping measures
- Network proxy/VPN interference
- TLS fingerprint rejection

The built-in 30s retry handles transient failures. **Post-fix:** `_safe_logout()` ensures the `finally` block never hangs after a broken connection, and the 30s socket timeout on IMAP connections prevents hangs during `select`/`search`/`fetch`. Persistent failures indicate cookie expiration or IP blocking.

---

> **重大修改须同步修改 CLAUDE.md** — 任何涉及新增模块、修改架构、改变配置契约、更新依赖、或改变启动流程的变更，必须在提交时同步更新本文件。
