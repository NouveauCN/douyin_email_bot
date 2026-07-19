# Email Bot for Douyin / Bilibili Video Download

邮箱机器人 —— 发邮件给机器人，自动下载抖音或 B 站视频到本地。

## 工作原理

```
你发邮件（含抖音/B站链接）→ 机器人轮询收件箱 → 下载视频 → 邮件回复结果
```

## 环境要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)（Python 包管理器）
- FFmpeg（Docker 镜像会自动安装）
- yutto CLI（Docker 镜像会隔离安装；本机开发只在需要 B 站下载时单独安装）
- 一个 QQ 邮箱账号（作为机器人邮箱）
- 无需特定操作系统（Windows / macOS / Linux 均可）

## 快速开始

### 1. 安装依赖

```bash
uv sync
```

### 2. 配置 QQ 邮箱

登录 QQ 邮箱网页版 → **设置** → **账户** → 找到 **POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务** → 开启 **IMAP/SMTP服务** → 获取**授权码**。

> ⚠️ 授权码不是你的 QQ 密码。开启服务后会显示一串 16 位字符，请妥善保存。

### 3. 获取抖音 Cookie

```bash
uv run f2 dy --auto-cookie chrome
```

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

`config.yaml` 只包含非敏感设置（服务器地址、端口等），隐私信息从 `.env` 加载。

如需限制发件人，编辑 `bot.allowed_senders`：

```yaml
bot:
  allowed_senders:
    - "your_email@qq.com"       # 允许发送下载请求的邮箱
```

### 5. 运行

```bash
uv run python main.py
```

### 6. 使用

从白名单邮箱向机器人邮箱发送邮件：
- **主题**：需包含"下载"（可自定义 `bot.subject_keyword`）
- **正文**：包含抖音或 B 站分享链接

机器人收到后会下载视频并回复邮件。

## B站下载

B站链接由 [yutto](https://github.com/yutto-dev/yutto) CLI 下载，支持 BV/av 投稿视频、番剧 ep/ss 以及 b23.tv 短链接。默认保存到 `downloads/bilibili/`。

注意：yutto 与 F2 的部分依赖版本约束冲突，因此 Docker 镜像会把 yutto 安装到独立的 `/opt/yutto` 虚拟环境，再通过 `yutto` 命令提供给机器人。不要把 yutto 加回主项目的 `requirements.txt` 或 `pyproject.toml`。

单个 B 站链接可能解析出多个视频文件（例如多 P、合集或启用批量模式的番剧/系列）。机器人回复会包含保存位置、文件数量，并列出前 10 个文件路径。

封面图片会保存到 `downloads/slides/`，文件名带 `bilibili_` 前缀，方便和抖音图集一起浏览。

普通公开视频通常不需要登录信息；如遇到登录、大会员或受限内容，在 `.env` 中配置：

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

抖音 cookie 有效期通常 **24-48 小时**，过期后下载会失败。机器人支持两种方式更新 cookie：

### 手动粘贴（主题：更新cookie）

向机器人邮箱发送邮件：
- **主题**：含"更新cookie"
- **正文**：粘贴新的 cookie 字符串

获取 cookie：浏览器登录 douyin.com → `F12` → 控制台 → `document.cookie` → 复制输出。

### 自动提取（主题：自动获取cookie）

向机器人邮箱发送邮件：
- **主题**：含"自动获取cookie"

机器人会依次尝试 Firefox → Chrome → Edge 浏览器，提取已登录的抖音 cookie 并自动更新。

> 提示：Windows 上建议安装 Firefox 并登录 douyin.com，Firefox 的 cookie 存储不加密，提取成功率最高。

## 配置说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `email.imap_server` | str | `imap.qq.com` | IMAP 收件服务器 |
| `email.imap_port` | int | `993` | IMAP SSL 端口 |
| `email.smtp_server` | str | `smtp.qq.com` | SMTP 发件服务器 |
| `email.smtp_port` | int | `587` | SMTP STARTTLS 端口 |
| `email.email` | str | `""` | **必填**（.env `EMAIL_ADDRESS`），机器人邮箱地址 |
| `email.password` | str | `""` | **必填**（.env `EMAIL_PASSWORD`），QQ 邮箱授权码 |
| `email.poll_interval` | int | `30` | 收件箱轮询间隔（秒） |
| `douyin.cookie` | str | `""` | **必填**（.env `DOUYIN_COOKIE`），抖音登录 cookie |
| `douyin.download_path` | str | `"./downloads"` | 视频下载目录 |
| `bilibili.download_path` | str | `"./downloads/bilibili"` | B站视频下载目录 |
| `bilibili.auth` | str | `""` | 可选（.env `BILIBILI_AUTH`），B站登录 cookie |
| `bilibili.auth_file` | str | `""` | 可选（env `BILIBILI_AUTH_FILE`），yutto 扫码登录认证文件 |
| `bilibili.video_quality` | int | `127` | yutto 视频清晰度，127=请求最高可用画质 |
| `bilibili.batch` | bool | `false` | 是否默认启用 yutto 批量下载 |
| `bilibili.yutto_bin` | str | `"yutto"` | yutto CLI 路径，可由 `BILIBILI_YUTTO_BIN` 覆盖 |
| `bot.allowed_senders` | list | `[]` | 允许的发件人邮箱（空=允许所有人） |
| `bot.subject_keyword` | str | `"下载"` | 触发下载的邮件主题关键词 |
| `bot.cooldown_seconds` | int | `5` | 同一发件人冷却时间 |
| `bot.commands.cookie_update` | str | `"更新cookie"` | 手动更新 cookie 的邮件主题关键词 |
| `bot.commands.cookie_auto` | str | `"自动获取cookie"` | 浏览器自动提取 cookie 的邮件主题关键词 |

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
