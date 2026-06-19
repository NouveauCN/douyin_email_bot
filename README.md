# WeChat Douyin Video Downloader

微信机器人 —— 接收抖音分享链接，自动下载视频到本地。

## 环境要求

- Windows 10/11 64-bit
- **微信 PC 版 3.9.12.17**（WeChatFerry 依赖特定版本，请关闭微信自动更新）
- Python 3.10+
- [uv](https://docs.astral.sh/uv/)（Python 包管理器）

## 快速开始

### 1. 安装依赖

```bash
uv sync
```

### 2. 获取抖音 Cookie

```bash
uv run f2 dy --auto-cookie chrome
```

或者从浏览器手动提取 cookie，填入 `config.yaml` 的 `douyin.cookie` 字段。

### 3. 配置

编辑 `config.yaml`：

```yaml
douyin:
  cookie: "你的抖音cookie"   # 必填
  download_path: "./downloads"  # 视频保存目录
```

### 4. 运行

```bash
uv run python main.py
```

首次运行会弹出微信登录二维码，用机器人账号扫码登录。登录成功后，向该微信发送抖音分享链接即可自动下载。

### 5. 使用

- 向机器人微信发送一条抖音分享链接（分享卡片或文字链接均可）
- 机器人会自动下载视频到 `downloads/` 目录
- 下载完成后机器人回复标题和保存路径

## 配置说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `wechat.host` | str | `null` | WeChatFerry RPC 地址（null=本地模式） |
| `wechat.port` | int | `10086` | RPC 端口 |
| `douyin.cookie` | str | `""` | **必填**，抖音登录 cookie |
| `douyin.download_path` | str | `"./downloads"` | 视频下载目录 |
| `douyin.folderize` | bool | `true` | 是否按用户分文件夹 |
| `bot.cooldown_seconds` | int | `5` | 同一发送者下载冷却时间 |
| `bot.allowed_senders` | list | `[]` | 允许的发送者 wxid（空=全部允许） |

## 常见问题

### 微信登录失败 / 消息收不到
请确认微信 PC 版本为 **3.9.12.17**。如果微信自动更新了新版本，需要降级。

### 抖音下载失败：未配置 cookie
抖音需要登录 cookie 才能下载视频。请运行 `uv run f2 dy --auto-cookie chrome` 获取。

### 抖音下载失败：视频不存在
链接对应的视频可能已被删除、设为私密或链接已过期。

### Protobuf 版本冲突
项目依赖的 `f2` 和 `wcferry` 对 protobuf 版本要求不同，`pyproject.toml` 中已配置 `override-dependencies` 解决此问题。

## 免责声明

本项目仅供个人学习和研究使用。请遵守微信和抖音的服务条款，尊重视频创作者的版权。使用本项目产生的任何后果由使用者自行承担。
