# Architecture Contract

Last reviewed: 2026-09-01

The service is a lightweight modular monolith for one private deployment.
Platform downloaders and the QQ transport are isolated, while task state and
entry-point orchestration remain in the bot process.  The goal is to make new
platforms and entry channels inexpensive to add without turning this Compose
deployment into a distributed system.  This document records the intended
seams so that further cleanup does not accidentally turn a facade into a
second source of truth.

### Design position and non-goals

The deployment intentionally has one Compose stack, one bot instance, and one
SQLite task/state database.  The Node QQ gateway and Flask file browser are
separate runtime boundaries because their dependencies and trust boundaries
differ; they are not independently scalable microservices.

This phase does not pursue high availability, horizontal scaling, service
discovery, an external message queue, distributed transactions, or a complex
dependency-injection framework.  It also does not split the SQLite database or
introduce a separate scheduler.  Lightweight interfaces, deterministic
transactions, idempotent outbox rows, and a small OS `flock` are sufficient for
the private single-instance workload.

## Runtime boundaries

```text
IMAP/SMTP                 Douyin HTTP/F2       Bilibili yutto
    |                            |                    |
    v                            +---------+----------+
 Email adapter                            v
    |                         Downloader registry/executor
    |                                      |
    |                         DownloadTaskService
    |                         (leases, workers, retry)
    |                                      |
    +------------------------------> MailStateStore
                                       (SQLite kernel)
                                             |
                         +-------------------+-------------------+
                         |                                       |
                   Email projector                        QQ projector
                         |                                       |
                    SMTP outbox                            QQ outbox

QQ Gateway (Node) --authenticated HTTP--> QQ bridge (bot process)

File Browser + embedded Web Login (separate Flask process)
    |                         |
    +--> runtime settings     +--> shared media tree
         (separate SQLite)
```

The bot process owns `EmailBot`, service composition, startup/shutdown,
settings revision handling, the internal QQ bridge, and the durable SQLite
connection.  `file_browser.py` owns browser routes and the embedded login
controller; it may use the runtime-settings database but never the bot's mail
state volume.  The optional Node gateway owns QQ SDK/session delivery state
and communicates only through the authenticated bridge.  IMAP, SMTP, F2,
HTTPX, yutto, Playwright, and FFmpeg are provider/process edges, not domain
dependencies of the task contracts.

## Dependency and ownership rules

The stable dependency direction is:

```text
source adapter -> task/source contract -> task facade -> task service
                                         -> platform adapter
task event -> sink projector -> sink outbox facade -> delivery provider
```

`download_types.py` is the public contract layer.  It must not import Flask,
SQLite, SMTP, IMAP, or a platform SDK.  `DownloaderRegistry` and
`DownloadExecutor` select and invoke a platform adapter; platform-specific
metadata, cookies, and subprocess details must not leak into the task service.

`EmailBot` is the composition and lifecycle owner, not the owner of every
domain operation.  Its long-term seams are:

- `EmailAdapter`: IMAP polling, UID reconciliation, URL extraction, sender
  policy, and durable mail-intake acknowledgement;
- `QQAdapter`: bridge request validation and source-specific intake mapping;
- `EmailProjector` and `QQProjector`: terminal event formatting and projection
  into their respective outboxes;
- `DownloadTaskService`: claims, leases, heartbeats, execution, retry policy,
  and terminal task events.

The adapters and projectors may depend on their facade and contract, but must
not reach through it to a raw SQLite connection or another sink's tables.
`EmailBot` may assemble and stop these components, but should not duplicate
their persistence or retry rules.

## Shared transactional kernel and facades

`MailStateStore` is deliberately a shared transactional kernel, not a public
domain API.  Keeping mailbox position, source bindings, tasks, event
consumption, leases, and outboxes in one SQLite transaction preserves the
failure atomicity required by this deployment.  The single-database choice is
intentional: do not replace it with distributed transactions or separate
stores merely to make the logical facades look like services.

The public access model is facade-based:

| Facade | Owns the public operations | Must not expose |
| --- | --- | --- |
| Task facade (`TaskStore`) | source-neutral submit/claim/heartbeat/complete/fail and task events | IMAP sequencing, SMTP formatting, QQ delivery details |
| Mail facade (mail adapter/projector API) | UID intake, `\\Seen` acknowledgement, email event projection and SMTP outbox delivery state | platform implementation and QQ tables |
| QQ facade (`QQStore`) | QQ message idempotency, reply-window policy, QQ event projection and QQ outbox leases | IMAP state, SMTP tables, Node SDK details |

The underlying store may implement these calls, but callers use the facade
surface.  In particular, no production path should use `service.store.state`
or invoke a sink-specific method as a fallback.  If a new operation is
needed, add it to the owning facade and test the contract there.

## Atomic intake and outbox projection

The following transitions are transaction boundaries:

1. Mail intake records `(mailbox, UIDVALIDITY, UID)`, source identity,
   normalized URL bindings, and task rows together.  `\\Seen` is applied only
   after the durable routing result is committed.
2. QQ intake validates one supported URL and atomically records its idempotent
   message/task binding plus the immediate acknowledgement outbox row.
3. A terminal task event is projected by exactly one sink consumer.  The
   projector inserts the sink outbox row and acknowledges the event in the
   same transaction.  The uniqueness key is `(task_id, sink/event)` (or the
   equivalent schema constraint), so replay cannot create a second notice.
4. Delivery uses a lease, bounded retries, and acknowledgement after the
   provider call.  An ambiguous SMTP response may produce a duplicate message;
   it must not cause a second durable task or outbox row.  Exhausted retries
   remain an explicit terminal delivery failure.

`EMAIL_SEND_REPLIES=0` suppresses creation of new SMTP outbox work while
continuing intake, execution, terminal state, and event consumption.  QQ
replies remain passive and are never converted to proactive messages after
their reply window expires.

## Durable path and legacy outcome policy

Durable mail processing is the default architecture.  The JSON retry files and
legacy polling path are a rollback source for migration, not a second feature
path to evolve.  Rollback is permitted only after SQLite intake, pending
`\\Seen` acknowledgements, durable tasks, and all relevant outbox/event work
are drained; an incomplete source stranded by a UIDVALIDITY change is retained
for operator reconciliation.

Both durable and legacy execution use the stateless executor and the same
platform result contract.  New behavior, retry classification, and terminal
notification rules must be added to the durable path first.  A terminal
outcome is one of:

- `succeeded`: all requested media completed;
- `partially_succeeded`: some media completed and the result records the
  failed count/files;
- `failed`: no usable result or a permanent failure, with a sanitized error
  and retry classification.

Transient failures are leased/retried; permanent failures and exhausted
retries are terminal.  Legacy cleanup happens only after the durable task and
its notification state are safe.  Removing the legacy source is intentionally
out of scope for this architecture phase.

## Media coordination

The bot's automatic media processing and the file browser's manual crop/dedup
routes can address the same NAS file.  A process-local Python lock (such as
the browser dedup lock) protects only one process and is not a coordination
contract.

The cooperative model is a lightweight OS `flock` shared by both writers.  For
this private deployment each operation takes one coarse media-tree lock plus a
resolved per-target lock.  The coarse lock deliberately trades unused
parallelism for a simple guarantee that directory deletion conflicts with
child-file processing; no hierarchical lock manager is warranted.  Locks are
acquired before inspection and held through temporary output creation and
atomic replacement, with a bounded timeout.  Sidecars remain hidden from
gallery operations and follow the same safe-root checks.

This lock cannot protect manual edits, NAS clients, or old versions that do
not participate.  It also does not make a crop semantically mergeable with a
concurrent delete; callers must surface a conflict and re-scan.  Until both
processes use one shared lock implementation, the residual risk is a
cross-process lost update even though individual writes are atomic.

## Resolved and residual coupling

Resolved or intentionally bounded:

- platform implementations are behind the registry/executor;
- yutto is isolated as a subprocess environment;
- QQ SDK/session state is outside the bot and the bridge is authenticated;
- managed settings and mail/task state use separate SQLite volumes;
- durable workers, leases, retry scheduling, event consumption, and outboxes
  are independent of the IMAP poll latency;
- source-neutral task/result contracts contain no transport or provider code.

Remaining by design or requiring follow-up:

- `MailStateStore` is one physical SQLite kernel by design; facades provide
  logical separation, not independent process/database availability;
- `EmailBot` remains the lifecycle/composition owner and will retain a small
  amount of compatibility glue until adapters/projectors are fully extracted;
- the legacy rollback path remains present and must not gain new behavior;
- media locking is cooperative; non-participating NAS clients remain outside
  the guarantee;
- Flask templates/routes are inline, and external queues/webhooks/IMAP IDLE are
  outside this lightweight phase unless a measured need appears;
- F2 import-order bootstrap remains a startup invariant.

## Verification checklist

For facade/projector or lifecycle changes, use mocked IMAP, SMTP, HTTP,
filesystem, browser, and subprocess edges.  At minimum verify:

```bash
git diff --check
uv run python -m compileall -q .
uv run pytest -q test_task_store.py test_qq_store.py test_durable_email.py
docker compose config --quiet
```

Add focused coverage for duplicate intake, replayed terminal events, outbox
lease expiry/retry, `\\Seen` failure, rollback refusal, and concurrent media
writers.  Do not use live downloads or real mail credentials as an architecture
check.
