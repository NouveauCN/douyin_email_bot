# 公共下载任务服务改造计划

状态：**已按审查意见实现并完成本地验收**

来源：参考任务 `codex://threads/01a0482b-4143-73b0-8781-f9ea26970b0b`。

## 目标

将下载任务从邮件入口中抽离，形成可被邮件、QQ 或其他入口复用的进程内
`DownloadTaskService`。入口只负责接收请求、提供上下文和发送结果；下载、
状态持久化、租约、重试和完成事件由公共服务负责。

目标调用链：

```text
入口适配器 → DownloadTaskService → DownloaderRegistry → 平台下载器
                         ↓
                    TaskStore / TaskEvent
                         ↓
              各入口自己的通知 outbox
```

本轮设计不新增 HTTP API，也不实现真实 QQ 入口；未来 QQ 与 Bot 同进程时，
直接调用 Python 接口即可。

## Sol 审查后的实现决策

本计划不能把 durable 任务服务和 legacy 回滚路径混成同一执行模型。实现采用
三层边界：`DownloaderRegistry`/`DownloadExecutor` 是无状态平台执行层，
`DownloadTaskService` 只负责 durable 任务的提交、租约、重试、事件和 worker，
legacy JSON 路径继续同步调用 `DownloadExecutor`，不启动 durable service。

`TaskRequest` 使用 `SourceRef(kind, external_id)` 形成幂等命名空间；共享 SQLite
保留现有 mail facade，并增加 `source_kind`、`external_source_id`、任务事件、
事件消费确认和 mail binding。终态写入与终态事件在同一事务内完成；邮件 projector
在同一事务中把事件投影为 SMTP outbox 并确认消费。旧版 Cookie 任务继续安全脱敏，
邮件不再接受 Cookie 更新/自动获取命令，Cookie 仍由 Web Login、`get_cookie.py`
和托管 `douyin.cookie` secret 提供。

## 公共接口与数据模型

新增模块：

- `download_types.py`
- `task_store.py`
- `download_task_service.py`

建议提供严格类型：

```python
TaskRequest(
    url: str,
    source_id: str,
    metadata: dict[str, JSONValue],
)

TaskSnapshot(
    task_id: int,
    url: str,
    platform: str,
    status: TaskStatus,
    attempts: int,
    result: DownloadResult | None,
    last_error: str | None,
    timestamps: ...,
)

DownloadResult(
    success: bool,
    filepath: str | None,
    files: tuple[str, ...],
    file_count: int,
    covers: tuple[str, ...],
    title: str | None,
    error: str | None,
    partial: bool,
    failed_count: int,
    failed_items: tuple[str, ...],
    retryable: bool,
)
```

`DownloadTaskService` 的主要接口：

```python
submit(request, on_update=None) -> TaskSnapshot
get(task_id) -> TaskSnapshot | None
start() -> None
shutdown() -> None
```

约束：

- 单次提交先支持一个 URL；批量提交以后再扩展。
- 同一 `source_id + URL` 重复提交必须幂等。
- 不同入口的相同 URL 默认建立独立任务，避免一个入口的通知状态影响另一个入口。
- `on_update` 只用于即时通知；回调异常不能影响任务状态。
- 任务状态和完成事件落库，是重启恢复和通知补发的权威依据。

## 任务服务实现范围

### 下载器注册与执行

- 定义 `Downloader` Protocol 和 `DownloaderRegistry`。
- 每个平台适配器提供平台名、URL 匹配能力和 `download(url)` 方法。
- 将现有 Douyin、Bilibili 的结果字典归一化为 `DownloadResult`，保留多文件、
  封面、部分成功和失败明细。
- 平台并发限制、通用重试、租约心跳和过期任务恢复移入任务服务。
- 未知 URL 返回结构化失败，不让入口层直接判断平台或实例化具体下载器。
- 新增平台时只需新增适配器并注册，不修改邮件处理流程。

现有 `bot.worker_count`、平台 worker 数量、lease、heartbeat 和 transient retry
配置继续沿用 `config_loader.py` 的配置路径，由任务服务读取或转换。

### 存储拆分

继续使用现有 `/app/state/mail_state.sqlite3`，避免改变部署卷和升级路径。

- 增加通用 `task_events` 表及对应访问方法。
- `TaskStore` 负责通用任务、租约、重试、结果和完成事件。
- `MailStateStore` 保留 IMAP mailbox position、UID/UIDVALIDITY、Seen ACK、
  邮件来源和兼容 facade。
- `smtp_outbox` 保留在邮件适配层；任务服务不得导入 SMTP 或调用邮件发送。
- 任务完成事件先持久化，再触发进程内回调。
- 邮件适配器消费完成事件，幂等生成现有 SMTP outbox 项。
- 保留旧 `MailStateStore` 任务方法作为兼容 facade，确保现有状态库和旧迁移
  逻辑可以升级。

### EmailBot 入口适配器

修改 `email_bot.py`：

- 收到邮件后提取单个 URL，构造 `TaskRequest` 并提交到任务服务。
- 只保存发件人、主题、收件人等非敏感通知上下文。
- 只负责格式化 `DownloadResult` 和写入 SMTP outbox。
- 保留现有 IMAP UID、Seen、幂等 intake 和回复行为。
- legacy JSON 路径继续保留作为回滚兼容模式，但通过任务服务的同步执行入口，
  不再直接依赖具体下载器。

### Cookie 责任调整

删除邮件触发的 Cookie 功能：

- 移除“更新 cookie”和“自动获取 cookie”邮件命令及其任务、解析、处理和自动刷新。
- 移除相关 `BotCommands`、managed settings、file browser 设置项和文档说明。
- 保留 `web_login.py` 写入托管 `douyin.cookie` 的能力。
- 保留 `get_cookie.py` 作为首次部署和人工故障恢复入口。
- 保留旧数据库中 `platform=cookie` 任务的安全清理和脱敏迁移。
- 更新启动及下载失败提示，优先指向 Web Login，CLI 作为备用。

注意：这意味着 Cookie 刷新不再是邮件入口或公共任务服务的隐式副作用；
Douyin 下载仍读取由 Web Login/托管设置提供的 Cookie。

## 测试与验收

新增或更新测试覆盖：

- Downloader registry 路由、未知 URL 拒绝和适配器注册。
- `TaskRequest` 幂等，以及跨入口重复 URL 的独立任务。
- `pending/running/succeeded/failed` 状态转换。
- 成功、永久失败、可重试失败、部分成功和过期租约恢复。
- 完成事件持久化、回调异常隔离和重启后的通知补发。
- EmailBot 只提交任务，不直接调用具体下载器。
- 邮件 Cookie 命令不再触发 Cookie 写入或 Cookie 任务。
- 现有 SQLite 状态库升级、旧任务和 SMTP outbox 兼容。
- Douyin、Bilibili、配置、durable email 和 settings 相关回归。

计划验收命令：

```bash
git diff --check
uv run python -m compileall .
uv run pytest -q
docker compose --profile login config --quiet
```

## 实施顺序

1. 先增加类型、TaskStore facade、任务事件表和下载器 registry，确保不改变旧入口行为。
2. 实现 `DownloadTaskService` 的提交、领取、执行、重试、心跳、恢复和事件回调。
3. 将 EmailBot durable 路径切换为任务服务，并保留旧状态库兼容方法。
4. 将 EmailBot legacy 路径切换为 `DownloadExecutor` 的同步兼容调用，保持
   JSON retry 的回滚边界，不启动 `DownloadTaskService`。
5. 删除邮件 Cookie 命令及其 UI/配置暴露，保留 Web Login 和安全迁移逻辑。
6. 补测试、更新文档，完成完整验证后再进行独立 PR 交付。

## 假设与暂缓事项

- 任务服务是进程内 Python 服务，不新增 HTTP API。
- 本轮只支持单 URL 提交，不支持批量入口协议。
- 回调不是可靠投递的唯一依据；任务结果和事件必须持久化。
- 不实现真实 QQ adapter，只保证未来可调用公共接口。
- 外部 webhook、消息队列和 IMAP IDLE 继续暂缓。
- Cookie 由 Web Login 作为主入口，`get_cookie.py` 作为备用入口。
