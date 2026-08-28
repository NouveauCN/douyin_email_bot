# Email Bot for Douyin / Bilibili Video Download

邮箱机器人 —— 发邮件给机器人，自动下载抖音或 B 站视频到本地。

## 工作原理

```
你发邮件（含抖音/B站链接）→ 机器人轮询收件箱 → 下载视频 → 邮件回复结果
```

## 环境要求

- Python 3.12（由 `.python-version`、`pyproject.toml` 和 Docker 统一约束）
- [uv](https://docs.astral.sh/uv/)（Python 包管理器）
- FFmpeg（Docker 镜像会自动安装）
- yutto CLI（Docker 镜像会隔离安装；本机开发只在需要 B 站下载时单独安装）
- 一个 QQ 邮箱账号（作为机器人邮箱）
- 无需特定操作系统（Windows / macOS / Linux 均可）

## 快速开始

### 1. 安装依赖

```bash
uv sync --frozen
```

### 2. 配置 QQ 邮箱

登录 QQ 邮箱网页版 → **设置** → **账户** → 找到 **POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务** → 开启 **IMAP/SMTP服务** → 获取**授权码**。

> ⚠️ 授权码不是你的 QQ 密码。开启服务后会显示一串 16 位字符，请妥善保存。

### 3. 获取抖音 Cookie

```bash
uv run python get_cookie.py
```

首次运行会打开 Firefox 供扫码登录；之后可用
`uv run python get_cookie.py --headless` 复用持久化登录状态。

### 4. 配置 .env 文件

复制模板并填入隐私信息：

```bash
cp .env.example .env
```

编辑 `.env`：

```env
EMAIL_ADDRESS=your_bot@qq.com
EMAIL_PASSWORD=你的QQ邮箱授权码
DOUYIN_COOKIE=你的抖音cookie
BILIBILI_AUTH="SESSDATA=...; bili_jct=..."  # 可选
```

`.env` 已被 `.gitignore` 排除，不会被提交到 Git。

### 5. 编辑 config.yaml（可选）

`config.yaml` 只包含非敏感的只读基线设置（服务器地址、端口等）。敏感信息首次启动
可从 `.env` 加载，日常修改推荐使用 file browser 的 Settings Tab。

如需限制发件人，编辑 `bot.allowed_senders`：

```yaml
bot:
  allowed_senders:
    - "your_email@qq.com"       # 允许发送下载请求的邮箱
```

### 6. 使用浏览器设置 Tab（推荐）

启动 `file_browser` 后打开 8081 端口的“设置” Tab，可以查看和修改邮箱、发件人
白名单、主题关键词、Douyin/Bilibili 登录信息、Cookie、重试和媒体处理等运行配置。
配置页面显示每项的来源；环境变量覆盖的部署项保持只读。邮箱密码、Douyin Cookie、
Bilibili 登录信息等 secret 只显示“已配置/未配置”，不会回显原值、掩码片段或长度。

Cookie-only 修改会热加载，不需要重启 bot。其他配置保存后 bot 会停止接收新邮件和领取
新任务，等待当前任务排空（最多 300 秒）后安全退出；Docker 的
`restart: unless-stopped` 会自动拉起新 bot。长时间 B 站任务如果超时被中断，会由
SQLite lease 到期恢复，不需要手动介入。设置页面本身不需要 Docker socket，也不能访问
bot 的 mail state 数据库。

`.env` 仍可用于首次启动、旧部署兼容和 Compose 插值；保存到设置 Tab 的 managed
settings 优先于 `.env` 和 `config.yaml`（实际注入的环境变量仍是最高优先级）。

### 7. 运行

```bash
uv run python main.py
```

### 6. 使用

从白名单邮箱向机器人邮箱发送邮件：
- **主题**：需包含"下载"（可自定义 `bot.subject_keyword`）
- **正文**：包含抖音或 B 站分享链接

机器人收到后会下载视频并回复邮件。

Docker 部署时，失败清单和自动重试队列保存在 bot 的 `state` named volume
中，不会因重建 bot 容器而丢失；对应文件位于容器内的
`/app/state/failed_links.txt` 和 `/app/state/pending_retries.json`。
邮件的 durable 状态和 SMTP outbox 也保存在同一 volume 的
`/app/state/mail_state.sqlite3`。收件采用 IMAP UID/UIDVALIDITY 幂等入库，
下载与回复由有界 worker 异步处理；SMTP 失败、进程重启或租约过期都会在
后续调度中恢复。旧 JSON 队列会保留为回滚源。迁移工具默认只检查不写入：

```bash
uv run python migrate_mail_state.py --pending ./pending_retries.json
uv run python migrate_mail_state.py --pending ./pending_retries.json --apply
```

如需回滚到旧的同步处理路径，先等待 SQLite intake、待确认的 `\\Seen`、任务和
outbox 清空，再设置
`BOT_DURABLE_MAIL_ENABLED=0`；若仍有在途 durable 工作，机器人会拒绝启动，
避免已标记 `\\Seen` 的邮件或待发通知被静默遗弃。不要在确认 durable 状态
与 outbox 已稳定前删除 JSON 队列。升级旧版 SQLite 状态时，历史 Cookie 任务会先
脱敏并标记为需通过 Web Login 或 CLI 更新。迁移还会安全清理 SQLite/WAL 中的旧页；任何外部数据库
备份仍需按既有秘密轮换策略处理。

设置 Tab 的 SQLite 数据保存在独立的 `runtime_settings` named volume，容器内路径为
`/app/runtime-settings/settings.sqlite3`。该卷只挂载给 `bot`、`file_browser` 和
`web_login`；`file_browser` 不挂载 `/app/state`，也不挂载 Docker socket。`config.yaml`
继续以只读方式挂载，旧 `.env` 继续保留用于兼容读取。

## 局域网 + Tailscale Web 访问

`file_browser` 和 `web_login` 不显示应用登录页，访问边界由 Docker
主机端口和 Tailscale 控制：Compose 同时提供本机回环、明确的可信家庭
LAN 地址和 Tailscale Serve 路径。`LAN_BIND_ADDRESS` 默认是
`192.168.1.94`，请按实际服务器地址修改，不要改成 `0.0.0.0`。
Tailscale 侧请用 Serve（不要用 Funnel）并用 ACL/Grants 只允许自己的设备
或用户访问。

服务仍会拒绝缺少或不匹配 Origin/Referer 的写请求，并限制上传大小、文件
数量、媒体并发、二维码生成和状态轮询；这些保护不需要额外登录操作。
出于设置接口会修改邮箱凭据和 Cookie，`PATCH /api/settings` 还要求显式配置
`FILE_BROWSER_ALLOWED_ORIGINS`；未配置时即使请求来自同源也返回 `403`。
如 Tailscale Serve 使用的地址与请求 Host 不同，可在 `.env` 中配置精确的
允许来源：

```env
# Replace 192.168.1.94 with the trusted LAN address when needed.
FILE_BROWSER_ALLOWED_ORIGINS=http://127.0.0.1:8081,http://localhost:8081,http://192.168.1.94:8081
WEB_LOGIN_ALLOWED_ORIGINS=http://127.0.0.1:8080,http://localhost:8080,http://192.168.1.94:8080
# Append (do not wildcard) the exact HTTPS Tailscale Serve origin:
# FILE_BROWSER_ALLOWED_ORIGINS=http://127.0.0.1:8081,http://localhost:8081,http://192.168.1.94:8081,https://your-machine.your-tailnet.ts.net
# WEB_LOGIN_ALLOWED_ORIGINS=http://127.0.0.1:8080,http://localhost:8080,http://192.168.1.94:8080,https://your-machine.your-tailnet.ts.net
```

不要把这两个服务绑定到 `0.0.0.0`、访客/IoT 网段或公网 Funnel；可信家庭
LAN 地址由 `LAN_BIND_ADDRESS` 显式指定。

## B站下载

B站链接由 [yutto](https://github.com/yutto-dev/yutto) CLI 下载，支持 BV/av 投稿视频、番剧 ep/ss 以及 b23.tv 短链接。默认保存到 `downloads/bilibili/`。

注意：yutto 与 F2 的部分依赖版本约束冲突，因此 Docker 镜像会从 `dependency-locks/yutto/uv.lock` 把完整锁定的 yutto 环境安装到独立的 `/opt/yutto`，再通过 `yutto` 命令提供给机器人。不要把 yutto 加回主项目的 `pyproject.toml`；主环境与 yutto 隔离环境分别使用各自的 `pyproject.toml` 和 `uv.lock`，Docker 只做冻结安装。

单个 B 站链接可能解析出多个视频文件（例如多 P、合集或启用批量模式的番剧/系列）。机器人回复会包含保存位置、文件数量，并列出前 10 个文件路径。

封面图片会保存到 `downloads/slides/`，文件名带 `bilibili_` 前缀，方便和抖音图集一起浏览。

普通公开视频通常不需要登录信息；如遇到登录、大会员或受限内容，可在 Settings Tab
中填写 B 站登录信息（旧部署也可继续在 `.env` 中配置）：

```env
BILIBILI_AUTH="SESSDATA=xxxxx; bili_jct=yyyyy"
```

## 自动裁掉纯色边缘

新下载的抖音/B站视频、图集图片和封面都会经过保守的自动裁边。处理器只检查从画布外缘连续延伸的近似同色行列，不会因为画面整体较暗就把黑底照片当成黑边；视频还要求分布在整个时长内的至少 90% 抽样帧达成一致。

对于占画面比例很大的边框，处理器会做第二级确认：如果所有抽样帧都存在稳定、成对的上下边框或左右边框，则自动裁剪；证据不足时保留文件，并要求人工确认。打开视频详情页，点击“检测并裁边”，页面会显示原尺寸、预计尺寸和四侧裁剪量，再决定是否继续。

裁剪成功时，原文件会保留为同目录下的 `*_original.bak`。检测、写入或 FFmpeg 处理失败时会恢复原件，且不会把已经完成的下载标记为失败。

对已有媒体可先预览，不会修改文件：

```bash
uv run python process_media.py /srv/nas_data/douyin_downloads
```

确认预览结果后才显式应用：

```bash
uv run python process_media.py /srv/nas_data/douyin_downloads --apply
```

命令行中需要人工确认的候选不会被 `--apply` 修改。确认后可针对单个文件执行：

```bash
uv run python process_media.py "/path/to/video.mp4" --apply --force-review
```

裁剪必须重新编码画面，但重新编码不会提升原始画质。视频优先沿用源视频码率，让裁剪后的文件大小接近原件；无法读取源码率时才使用保守的质量参数。

## Cookie 管理

抖音 cookie 有效期通常 **24-48 小时**，过期后下载会失败。Cookie 不再通过邮件命令
更新。推荐启动 `web_login` 后使用二维码登录；首次部署或故障恢复也可以运行
`uv run python get_cookie.py`。两种入口都把 Cookie 保存到托管 settings，运行中的
Bot 会通过 `douyin.cookie` 的 hot reload 立即读取，Cookie 内容不会回显到页面、日志
或邮件任务状态。

## 配置说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `email.imap_server` | str | `imap.qq.com` | IMAP 收件服务器 |
| `email.imap_port` | int | `993` | IMAP SSL 端口 |
| `email.smtp_server` | str | `smtp.qq.com` | SMTP 发件服务器 |
| `email.smtp_port` | int | `587` | SMTP STARTTLS 端口 |
| `email.email` | str | `""` | **必填**（managed settings 或 `.env` `EMAIL_ADDRESS`），机器人邮箱地址 |
| `email.password` | str | `""` | **必填**（managed settings 或 `.env` `EMAIL_PASSWORD`），QQ 邮箱授权码 |
| `email.poll_interval` | int | `30` | 收件箱轮询间隔（秒） |
| `email.smtp_timeout` | int | `30` | SMTP 连接与发送超时（秒）；由 managed settings/YAML 控制，只有显式外部环境变量注入时锁定 |
| `douyin.cookie` | str | `""` | **必填**（managed settings 或 `.env` `DOUYIN_COOKIE`），抖音登录 cookie |
| `douyin.download_path` | str | `"/srv/nas_data/douyin_downloads"` | 视频下载目录（提交配置） |
| `bilibili.download_path` | str | `"/srv/nas_data/douyin_downloads/bilibili"` | B站视频下载目录（提交配置） |
| `bilibili.auth` | str | `""` | 可选（managed settings 或 `.env` `BILIBILI_AUTH`），B站登录 cookie |
| `bilibili.auth_file` | str | `""` | 可选（env `BILIBILI_AUTH_FILE`），yutto 扫码登录认证文件 |
| `bilibili.video_quality` | int | `127` | yutto 视频清晰度，127=请求最高可用画质 |
| `bilibili.batch` | bool | `false` | 是否默认启用 yutto 批量下载 |
| `bilibili.yutto_bin` | str | `"yutto"` | yutto CLI 路径，可由 `BILIBILI_YUTTO_BIN` 覆盖 |
| `bot.allowed_senders` | list | `[]` | 允许的发件人邮箱（空=允许所有人） |
| `bot.subject_keyword` | str | `"下载"` | 触发下载的邮件主题关键词 |
| `bot.cooldown_seconds` | int | `5` | 同一发件人冷却时间 |
| `bot.transient_pending_file` | str | `./pending_retries.json` | 自动重试队列文件；可由 `BOT_TRANSIENT_PENDING_FILE` 覆盖 |
| `bot.transient_failed_file` | str | `./failed_links.txt` | 重试耗尽链接的失败清单；可由 `BOT_TRANSIENT_FAILED_FILE` 覆盖 |
| `bot.durable_mail_enabled` | bool | `true` | 启用 SQLite durable intake、worker 和 SMTP outbox；可由 `BOT_DURABLE_MAIL_ENABLED` 覆盖 |
| `bot.state_db` | str | `./state/mail_state.sqlite3` | SQLite 状态库；Docker 中固定在 `/app/state` named volume |
| `bot.worker_count` | int | `2` | 全局下载 worker 数；由 managed settings/YAML 控制，只有显式外部环境变量注入时锁定 |
| `bot.douyin_worker_count` / `bilibili_worker_count` | int | `1` / `1` | 各平台并发上限；由 managed settings/YAML 控制，只有显式外部环境变量注入时锁定 |
| `bot.lease_seconds` / `heartbeat_seconds` | int | `300` / `30` | worker 租约与心跳周期；由 managed settings/YAML 控制，只有显式外部环境变量注入时锁定 |
| `bot.outbox_retry_attempts` | int | `5` | SMTP outbox 最大重试次数；由 managed settings/YAML 控制，只有显式外部环境变量注入时锁定 |
| `FILE_BROWSER_ALLOWED_ORIGINS` | str | 当前请求 origin | 文件浏览器精确允许来源（逗号分隔） |
| `WEB_LOGIN_ALLOWED_ORIGINS` | str | 当前请求 origin | QR 服务精确允许来源（逗号分隔） |
| `DOUYIN_SHORT_LINK_CA_BUNDLE` | str | 系统 CA | 私有 CA bundle 路径；不配置时使用正常证书校验 |

## 常见问题

### 连接邮箱失败
- 确认已开启 QQ 邮箱的 IMAP/SMTP 服务
- 确认 `.env` 中 `EMAIL_PASSWORD` 填写的是**授权码**而不是 QQ 密码
- 确认 `.env` 中 `EMAIL_ADDRESS` 正确

### 抖音下载失败
- 确认 `.env` 中 `DOUYIN_COOKIE` 已正确填写
- 抖音 cookie 有时效性，过期后需重新获取

### B站下载失败
- Docker 部署时重新构建镜像，确认镜像内已安装 yutto 和 FFmpeg
- 本机直接运行且需要 B站下载时，需在主项目环境外单独安装 yutto CLI
- 登录/大会员/受限内容需配置 `.env` 中的 `BILIBILI_AUTH`，或用 `yutto auth login --auth-file` 生成认证文件

### 自动裁边没有处理某个文件
- 处理器宁可少裁也不碰主体；边缘色差过大、裁剪区域过大或视频抽样帧意见不一致时会跳过
- 本机处理视频需要同时安装 `ffmpeg` 和 `ffprobe`
- 已存在对应的 `*_original.bak` 时不会重复处理
- 可在视频详情页点击“检测并裁边”查看候选范围并人工确认

### 邮件发不出去
- QQ 邮箱 SMTP 有频率限制，建议 `poll_interval` 不小于 30 秒

## 其他邮箱

默认配置适用于 QQ 邮箱。其他邮箱只需修改 `email:` 配置段即可：

```yaml
# 163 邮箱示例
email:
  imap_server: "imap.163.com"
  imap_port: 993
  smtp_server: "smtp.163.com"
  smtp_port: 465

# Gmail 示例（需开启两步验证 + App Password）
email:
  imap_server: "imap.gmail.com"
  imap_port: 993
  smtp_server: "smtp.gmail.com"
  smtp_port: 587
```

## 免责声明

本项目仅供个人学习和研究使用。请遵守相关平台的服务条款，尊重视频创作者的版权。

## 后续计划

安全加固、依赖锁定、运行时升级和结构改造的分阶段计划见
[`ROADMAP.md`](ROADMAP.md)。
