# Email Bot for Douyin Video Download

邮箱机器人 —— 发邮件给机器人，自动下载抖音视频到本地。

## 工作原理

```
你发邮件（含抖音链接）→ 机器人轮询收件箱 → 下载视频 → 邮件回复结果
```

## 环境要求

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)（Python 包管理器）
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

### 4. 编辑配置文件

编辑 `config.yaml`：

```yaml
email:
  email: "your_bot@qq.com"     # 机器人邮箱地址
  password: "你的QQ邮箱授权码"    # 不是 QQ 密码！

douyin:
  cookie: "你的抖音cookie"       # 上一步获取的

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
- **正文**：包含抖音分享链接

机器人收到后会下载视频并回复邮件。

## 配置说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `email.imap_server` | str | `imap.qq.com` | IMAP 收件服务器 |
| `email.imap_port` | int | `993` | IMAP SSL 端口 |
| `email.smtp_server` | str | `smtp.qq.com` | SMTP 发件服务器 |
| `email.smtp_port` | int | `587` | SMTP STARTTLS 端口 |
| `email.email` | str | `""` | **必填**，机器人邮箱地址 |
| `email.password` | str | `""` | **必填**，QQ 邮箱授权码 |
| `email.poll_interval` | int | `30` | 收件箱轮询间隔（秒） |
| `douyin.cookie` | str | `""` | **必填**，抖音登录 cookie |
| `douyin.download_path` | str | `"./downloads"` | 视频下载目录 |
| `bot.allowed_senders` | list | `[]` | 允许的发件人邮箱（空=允许所有人） |
| `bot.subject_keyword` | str | `"下载"` | 触发下载的邮件主题关键词 |
| `bot.cooldown_seconds` | int | `5` | 同一发件人冷却时间 |

## 常见问题

### 连接邮箱失败
- 确认已开启 QQ 邮箱的 IMAP/SMTP 服务
- 确认 `email.password` 填写的是**授权码**而不是 QQ 密码
- 确认服务器地址和端口正确

### 抖音下载失败
- 确认 `douyin.cookie` 已正确填写
- 抖音 cookie 有时效性，过期后需重新获取

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
