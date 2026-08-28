# Security and Upgrade Roadmap

Last reviewed: 2026-08-28

This document records the planned follow-up work after the repository-wide
audit merged in PR #14. Each phase should be delivered as a separate pull
request so security, dependency, deployment, and structural changes can be
validated and rolled back independently.

## Delivery Order

Durable mail intake and delivery must precede any low-latency event optimization.
IMAP IDLE, if adopted, is only a wake-up hint: it must not replace periodic UID
reconciliation or the existing polling fallback. External mail webhooks and
message queues remain deferred until the local workflow needs them.

### Phase 1: Web Security Boundary (P0)

Protect both Flask applications before expanding their network exposure.

- Require network-layer authentication for `file_browser` and `web_login`.
  Application login is intentionally omitted for the personal LAN + Tailscale
  deployment, but only when the services are reachable through the controlled
  Tailscale boundary.
- Require CSRF protection and validate `Origin`/`Referer` on every mutating
  file-browser request, including upload, delete, crop, and duplicate actions.
- Keep read-only comics paths outside every mutating download operation.
- Add upload size, processing timeout, and bounded-concurrency limits.
- Bind `web_login` to loopback or an explicit management address by default.
- Add rate limits to QR generation and login-status polling.
- Keep QR and status responses non-cacheable and never return cookie contents.

Preferred access boundary: use Tailscale ACLs/Grants and Tailscale Serve (or an
equivalent authenticated reverse proxy), plus an explicitly configured trusted
home-LAN address when local devices need direct access. Do not bind all
interfaces or expose them through guest/IoT networks or Funnel. If a future
deployment cannot enforce that boundary, use a session-based login backed by a
separately managed secret; do not transmit a reusable Basic or bearer
credential over plain HTTP.

Acceptance criteria:

- Requests outside the Tailscale or explicitly trusted home-LAN boundary cannot
  connect; cross-origin and missing-source mutating requests return `403`.
- Valid same-origin browser flows can still upload, delete, crop, and resolve
  duplicates without an application login prompt.
- QR and status endpoints cannot be reached outside the boundary, reject
  missing/untrusted sources, and remain non-cacheable.
- Oversized uploads and excess concurrent processing fail predictably without
  exhausting memory, disk, or Flask workers.

Implementation status (2026-08-23; PRs #16, #17, #18, and #19):

- `web_login.py` now defaults to loopback, validates exact same-origin or
  configured origins, limits QR generation to 5 requests/minute and status
  polling to 120 requests/minute by default, and keeps `no-store` plus cookie
  redaction.
- `file_browser.py` now rejects missing/cross-origin mutating requests,
  limits uploads to 2 GiB and 10 files by default, and bounds media-changing
  work to two concurrent jobs. Existing FFmpeg and thumbnail subprocess
  timeouts remain in force.
- `docker-compose.yml` publishes 8080/8081 only to loopback plus the explicit
  `LAN_BIND_ADDRESS` (default `192.168.1.94`); it never binds `0.0.0.0`.
- The host's Tailscale Serve entries for 8080/8081 forward to the loopback
  ports and remain tailnet-only. Tailscale ACL/Grants are managed outside this
  repository because the local CLI cannot edit tailnet policy.
- Application login was intentionally not added for the personal trusted-LAN
  plus Tailscale deployment. If the LAN boundary widens beyond the trusted
  home network, add an authenticated proxy or application session before
  exposing these writable services.

### Phase 2: Short-Link Transport Security (P0/P1)

Status: **Completed** in PR #20 (`b426f7f`).

Implementation delivered:

- Short-link input and transport are HTTPS-only; the old HTTP fallback and
  `verify=False` behavior were removed.
- Redirects are accepted only for approved Douyin hosts with exact
  `/video/<id>`, `/note/<id>`, and `/share/.../<id>` paths. Credentials,
  fragments, malformed URLs, and non-standard ports are rejected.
- System CA verification is used by default. A private CA is supported only
  through the explicit `DOUYIN_SHORT_LINK_CA_BUNDLE` setting.
- Only validated HTTPS targets are cached. Cache entries now carry the
  `https-validated-v1` schema marker, so legacy entries are ignored and
  re-resolved rather than trusted.
- Added mocked coverage for valid redirects, HTTP/foreign/malformed targets,
  TLS failures, CA configuration, cache isolation, and no-cache-on-failure
  behavior. Network failures continue through the existing retry path.

- Resolve Douyin short links over HTTPS with normal CA verification by default.
- Remove automatic HTTP fallback and unconditional `verify=False`.
- Validate redirect hosts and accepted aweme URL/ID shapes before caching.
- If a deployment needs a private CA, configure that CA explicitly.
- Any temporary insecure compatibility mode must be opt-in, emit a prominent
  warning, and must not populate the persistent short-link cache.

Acceptance criteria:

- Mocked valid HTTPS redirects resolve and cache normally.
- Certificate failures, HTTP redirects, foreign hosts, and malformed targets
  are rejected without cache writes.
- Network failures remain contained by the existing retry queue.

### Phase 3: Reproducible Runtime and Dependency Upgrade (P1)

Status: **Completed** in PR #33 (2026-08-26).

Implementation delivered:

- Python is constrained to the 3.12 series locally and in Docker, with a
  checked-in `.python-version`.
- Exact direct dependencies are declared in `pyproject.toml`; `uv.lock` is the
  sole complete runtime lock and Docker installs it with pinned uv 0.12.5 and
  `uv sync --frozen`. The duplicate hand-maintained `requirements.txt` was
  removed.
- Playwright and python-dotenv were upgraded to 1.62.0 and 1.2.3. F2 remains
  exactly pinned to 0.0.1.7, which requires httpx 0.27.2 and PyYAML 6.0.2.
- yutto remains isolated in `/opt/yutto`; its exact 2.2.0 package and complete
  transitive set have a separate frozen lock under `dependency-locks/yutto/`.
- Frozen local installation, the mocked test suite, F2 bootstrap coverage,
  Playwright Firefox startup, and Compose configuration pass. The `bot` and
  `file_browser` Docker images built successfully, and both services were
  rebuilt and restarted from the merged `main` revision.

Standardize local development and Docker on Python 3.12 for the next upgrade
cycle. Do not jump to Python 3.14 until F2, Playwright, FFmpeg, and media tests
have been verified together.

Use `pyproject.toml` and `uv.lock` as the authoritative dependency sources.
Docker installs a frozen runtime set directly from the lock; hand-maintained
dependency ranges must not remain a second source of truth. Keep yutto in its
isolated environment with its separate lock.

Target package review:

| Package | Audited lock | Target/action |
| --- | --- | --- |
| Flask | 3.1.3 | Pinned in `pyproject.toml` and `uv.lock` |
| Pillow | 12.3.0 | Pinned in `pyproject.toml` and `uv.lock` |
| Playwright | 1.60.0 → 1.62.0 | Upgraded and locked |
| httpx | 0.27.2 | Kept at 0.27.2 because F2 0.0.1.7 prevents 0.28.x |
| python-dotenv | 1.2.2 → 1.2.3 | Upgraded and locked |
| F2 | 0.0.1.7 | Pinned exactly; upgrade deferred pending bootstrap/download tests |
| yutto | 2.2.0 | Pinned exactly in the isolated environment lock |
| colorama | 0.4.6 | Pinned in `pyproject.toml` and `uv.lock` |

Version references: [Flask](https://pypi.org/project/Flask/),
[Pillow](https://pypi.org/project/pillow/),
[Playwright](https://pypi.org/project/playwright/),
[HTTPX](https://pypi.org/project/httpx/),
[python-dotenv](https://pypi.org/project/python-dotenv/),
[F2](https://pypi.org/project/f2/), and
[yutto](https://pypi.org/project/yutto/).

Acceptance criteria:

- Add `.python-version` and make local/CI/Docker interpreter choices explicit.
- `uv lock --check` and frozen installation succeed from a clean checkout.
- Docker rebuilds install the same resolved versions on repeated builds.
- Playwright Firefox starts, F2 bootstrap tests pass, and yutto remains isolated.
- The complete mocked test suite and Docker Compose checks pass.

### Phase 4: Secret Persistence and Health Checks (P1)

Move mutable secrets from the current single-file `.env` bind mount into the
managed settings database specified in Phase 6. The database is shared with
`file_browser` only for the Settings tab's controlled read/write API; it must
never expose secret values. Replace duplicated `.env` writers with one helper
that provides:

- inter-process file locking;
- control-character validation;
- preserved formatting and unrelated keys;
- temporary write, flush, `fsync`, mode `0600`, and atomic replacement;
- explicit failure reporting before in-memory cookie state is changed.

Do not introduce `os.replace()` while `.env` itself is still a bind-mount
target; replacing a mounted file is not portable across container runtimes.
The legacy `.env` remains a compatibility/bootstrap input and is not the normal
mutable settings store.

Also add:

- `/healthz` and `/readyz` for both Flask services;
- a bot heartbeat that proves the polling loop is progressing;
- Docker Compose health checks and startup grace periods;
- graceful shutdown that stops intake, drains bounded workers within a deadline,
  and leaves leased tasks recoverable after restart;
- bounded request and subprocess timeouts.

Acceptance criteria:

- Concurrent and interrupted secret updates never create an empty or partial
  file and never lose unrelated keys.
- Host and container readers observe the same updated secret.
- Container health reflects application readiness, not only process existence.

### Phase 5: Durable Mail Processing and Delivery (P1)

Implementation status: **Completed in PR #37; PR remains open and unmerged**
(2026-08-27).

Sol Senior final high-risk review: **FINAL PASS** (2026-08-27).

- Added a WAL-backed `sqlite3` state store under the bot state volume with
  mailbox UID/UIDVALIDITY generations, source and normalized-URL idempotency,
  task leases/heartbeats, recovery, and an atomic task-result/SMTP-outbox
  transition.
- IMAP intake now uses UID search/fetch and acknowledges `\\Seen` only after
  source routing is marked complete. Failed flag updates remain pending and
  are retried during the next reconciliation; incomplete routing is never
  acknowledged.
- Bounded download workers and a separate SMTP outbox worker decouple slow
  media work and delivery from the 30-second polling coordinator. Douyin and
  Bilibili capacity are independently limited. The reusable
  `DownloadTaskService` owns execution, leases, retries, and completion events;
  EmailBot owns only the mail event projector and SMTP outbox.
- Legacy JSON retries remain intact as a rollback source and are imported
  idempotently. `migrate_mail_state.py` is dry-run by default; after SQLite
  intake, tasks, outbox, and pending `\\Seen` acknowledgements drain, set
  `BOT_DURABLE_MAIL_ENABLED=0` to use the legacy path. Startup refuses rollback
  while durable work or unconsumed email events remain. Email Cookie commands
  are no longer accepted; pre-v2 cookie tasks are redacted and terminally
  failed so operators must use Web Login or the CLI. The migration securely rebuilds
  SQLite and checkpoints/truncates its WAL; external database backups still
  require the normal secret-rotation policy.
- A terminal durable failure removes its mirrored legacy JSON retry only after
  the failed task and notification are durably recorded, preventing duplicate
  execution if the operator later rolls back.
- Lease recovery, UIDVALIDITY changes, duplicate intake, outbox idempotency,
  SMTP retry, `\\Seen` failure, and legacy migration are covered by mocked
  fault-injection tests. External webhooks, queues, and IMAP IDLE remain
  deferred.

Make mail intake reliable before optimizing its trigger latency. Keep the
30-second polling loop as the default while this phase is being introduced.

- Persist mailbox identity and processing position as `(mailbox, UIDVALIDITY,
  UID)`; treat the current in-memory `_seen_ids` and sequence numbers only as
  legacy behavior during migration. Reconcile safely after UIDVALIDITY changes.
- Add a SQLite task-state store under the bot's persistent `state` volume, not
  the NAS media mount. Use unique idempotency constraints that distinguish a
  source message from each normalized URL or media item it creates.
- Decouple IMAP receipt from media download and SMTP delivery through bounded
  workers. A slow or failed Douyin/Bilibili download must not block intake,
  retries, or maintenance tasks.
- Add expiring worker leases and heartbeats so interrupted tasks become
  recoverable after process or container restart. Keep Firefox cookie access
  serialized and apply explicit per-platform concurrency limits.
- Add a durable SMTP outbox with send state, retry status, and recovery after
  interruption. Do not delete a retry record until the corresponding durable
  notification state is safe.
- Run transient retry processing, media-backup cleanup, and other maintenance
  work from independent schedulers rather than coupling them to one IMAP poll
  iteration.
- Add fault-injection coverage for disconnects, UIDVALIDITY changes, duplicate
  delivery, crashes between state transitions, lease expiry, `\Seen` failures,
  and SMTP failures or ambiguous responses.
- Explicitly defer external mail webhooks and external message queues; first
  stabilize the local SQLite-based workflow.

Acceptance criteria:

- A long-running download does not prevent new mail from being durably accepted
  into the task store.
- Process and worker restarts recover all leased work without silent loss, and
  duplicate IMAP events do not create duplicate download jobs or notifications.
- `\Seen` is applied only after durable intake succeeds; failed SMTP delivery
  remains recoverable through the outbox.
- Retry and cleanup tasks run on their own schedules even when no new mail
  arrives, and the state database survives bot container replacement.
- Mocked integration and fault-injection tests cover the failure transitions
  above without live email, browser, or media downloads.

Rollback point: retain the existing polling intake behind a feature flag while
the SQLite state store is additive and its migration is reversible. Do not
remove the existing retry files until recovery and rollback have been tested.

Optional follow-up:

- Add IMAP IDLE as a configurable wake-up layer after the durable workflow is
  stable. Check server capability, periodically leave and re-enter IDLE,
  reconnect with exponential backoff, and always perform periodic UID
  reconciliation. Fall back to polling when IDLE is unsupported or unhealthy.
- Adopt this only if a measured mail-trigger latency target justifies the
  additional long-connection complexity; otherwise retain 30-second polling.

### Phase 6: Browser Runtime Settings Control Plane (P1)

Expose supported runtime configuration through a Settings tab in the trusted
LAN file browser while keeping deployment boundaries and secrets explicit.

- Persist managed settings and revision metadata in an independent
  `runtime_settings` named volume at `/app/runtime-settings/settings.sqlite3`.
  Mount it only into `bot`, `file_browser`, and `web_login`; never share the bot
  mail-state volume or Docker socket with `file_browser`.
- Keep `config.yaml` read-only and retain `.env` for bootstrap, legacy reads, and
  Compose interpolation. Resolve supported values as
  `environment > managed settings > legacy .env > config.yaml > defaults`.
  Compose must not inject default `BOT_WORKER_COUNT`, per-platform worker,
  lease/heartbeat, or SMTP outbox environment values; managed settings/YAML
  control those fields unless an external process explicitly injects an
  environment override, which locks the field in the UI.
- Display sources and editability in the browser. Secret fields are write-only:
  responses and logs report only whether a value is configured.
- Hot-reload Cookie-only changes. For other changes, stop intake and new claims,
  drain active workers for at most 300 seconds, then exit; Docker
  `restart: unless-stopped` automatically starts the bot with the new revision.
  Lease expiry recovers interrupted long Bilibili jobs.
- Preserve the existing loopback + explicit trusted-LAN + Tailscale Serve
  boundary and Origin/Referer checks. Settings writes must use the same
  no-store and CSRF-style protections as other mutating browser routes. Require
  an explicit `FILE_BROWSER_ALLOWED_ORIGINS` allowlist for Settings PATCH;
  missing configuration returns `403`, even for same-origin requests.

Acceptance criteria:

- A browser user can inspect and update all supported email, allowlist,
  downloader, retry, media, and cookie settings without editing files.
- Environment-locked and deployment-only fields are visibly read-only; no
  credential, Cookie, or Bilibili auth value appears in HTML, JSON, logs, or
  error text.
- Settings PATCH is unavailable until exact localhost/LAN origins are configured
  (and any Tailscale Serve origin is explicitly appended); wildcard origins are
  rejected.
- Cookie-only saves do not restart the bot; all other saves converge to the
  requested revision automatically, including after a drain timeout or bot
  crash, without manual container intervention.
- The file browser has access to neither `/app/state` nor Docker control APIs,
  and the three services can restart while preserving managed settings.

### Phase 7: Production Serving and Maintainability (P1/P2)

- Replace Flask's development server with a production WSGI server.
- Initially use one worker because file-browser dedup state and the Firefox
  browser lock are process-local; add threads only after focused concurrency
  tests.
- Reuse the internal bounded worker and task-state model for long FFmpeg work;
  do not introduce an external webhook or message queue in this phase.
- Add CI for Python 3.12, tests, compilation, diff checks, Compose validation,
  image build smoke tests, linting, and dependency/security scans.
- After the security and deployment work is stable, split inline templates and
  route logic into app factories, route modules, services, templates, and
  static assets.
- Make the thumbnail cache path configurable and verify cache permissions and
  atomic generation.

## Decisions Required Before Implementation

1. Decided for the current deployment: use
   Tailscale ACLs/Grants plus Tailscale Serve and the explicitly configured
   trusted home-LAN address; application-managed sessions are required if that
   boundary is widened.
2. Decided in Phase 6: Cookie-only managed-setting updates hot-reload; other
   settings request an automatic bot drain and Docker restart of at most 300
   seconds. Secrets remain write-only in the browser UI.
3. Decided: reject broken TLS by default; support only an explicitly configured
   private CA through `DOUYIN_SHORT_LINK_CA_BUNDLE`.
4. Decided: install the authoritative `uv.lock` directly with a pinned uv
   version in the image; do not commit a second exported requirements artifact.
5. Decided in P5: `\Seen` means durable intake, not business completion. The
   final success or failure notification is represented by the SMTP outbox.
6. Decided in P5: state is stored at `/app/state/mail_state.sqlite3`; a
   UIDVALIDITY change starts and archives a new mailbox generation. The first
   run or a new UID generation performs full UID reconciliation; normal polls
   use the high-water range plus `UNSEEN`. Tasks are unique by source message
   and normalized URL, use 300-second leases with 30-second heartbeats, and
   legacy JSON migration is idempotent and reversible. Rollback is refused
   while intake, pending `\\Seen` acknowledgements, durable tasks, or outbox
   work remains; an incomplete source stranded by a UIDVALIDITY change is
   retained for operator reconciliation rather than auto-acknowledged.
7. Decided in P5: two global worker slots are available, with one each for
   Douyin and Bilibili; sender cooldowns and Firefox cookie access are
   serialized. SMTP uses a stable Message-ID, explicit timeout, bounded retry,
   and may duplicate delivery after an ambiguous remote response; after retry
   exhaustion the outbox enters terminal `failed` state.
8. Latency target: measure whether the service needs sub-30-second or roughly
   P95-under-5-second intake latency. Enable IMAP IDLE only if that target
   justifies its long-connection and recovery complexity.

## Verification Baseline for Every Phase

```bash
git diff --check
uv run pytest -q
uv run python -m compileall -q .
uv lock --check
docker compose --profile login config --quiet
```

Do not use live email, browser, or media downloads as routine CI checks. Use
mocked network, IMAP, SMTP, browser, filesystem, and subprocess behavior.
