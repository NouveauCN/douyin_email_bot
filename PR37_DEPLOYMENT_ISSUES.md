# PR #37 部署实测问题记录

## 基本信息

- 记录时间：2026-08-27（Asia/Shanghai）
- PR：[NouveauCN/douyin_email_bot#37](https://github.com/NouveauCN/douyin_email_bot/pull/37)
- 被测提交：`95d36391f79a6e2df1ec30b584b8187f6a572870`
- PR 基线：`03d6d6eb29b33801b1f120011175ebd8f2bc7ed8`
- 当时最终部署：PR #37 基线；PR #37 保持开放、未合并
- 当前状态：PR #37 已于 2026-08-28 合并，合并提交为 `b5e11169b253907fb45a0659d49ba544879fe98f`

> 本文保留首次部署失败的历史现场。下述“未通过”“回退”和“保持开放”
> 均描述 2026-08-27 当次验证，不代表 PR #37 的当前状态。

## 解决状态

- 未知/非法邮件字符集现在使用安全 fallback，不再中断轮询。
- 单封路由异常会按 UID 隔离并持久化，不影响同轮后续邮件。
- durable 首次启动及 UIDVALIDITY 变化不会重放历史已读邮件。
- 修复后完整测试为 147 项通过、1 项跳过；真实 QQ 邮件闭环完成
  IMAP 摄取、3 张抖音图文下载、durable task 成功、SMTP outbox 发送及
  对端收信确认。
- 后续部署暴露的 Firefox profile 字符串路径类型问题由独立修复处理，
  不改变本文记录的原始字符集故障结论。

## 结论

PR #37 本次真实邮件验证未通过。服务可以启动，Cookie、SQLite 状态初始化和 IMAP 连接均正常，但处理邮件主题时遇到 `unknown-8bit` 字符集标签，抛出未处理的 `LookupError`，导致当前轮询周期失败。

因此本轮没有保留 PR #37 部署，而是回退到 PR 的精确 `base/main` 提交。回退版本随后成功处理同一封测试邮件，完成一张图文资源的下载并发送回复。

## 实测过程

1. 从 PR #37 head 构建并启动 `bot`、`file_browser` 两个服务。
2. 启动日志显示 Cookie 已加载、待重试队列为空、IMAP 轮询已开始，容器均正常运行。
3. 发送一封真实测试邮件并观察 bot 日志。
4. PR 版本出现轮询异常后，停止 PR 版本服务。
5. 切换到 `03d6d6e` 并重新构建启动两个服务。
6. 回退版本成功下载测试邮件中的 1 张图文资源到 `slides/`，并向测试发件人发送回复。

## 发现的问题

### P0：未知邮件字符集会中断 durable poll cycle

现象：

```text
EmailBot: Unexpected error during poll cycle
LookupError: unknown encoding: unknown-8bit
```

调用路径为：

```text
_poll_once_durable()
  -> _durably_accept_email()
    -> _decode_str(msg.get("Subject", ""))
      -> bytes.decode("unknown-8bit", errors="replace")
```

对应实现位于 `email_bot.py` 的 `_decode_str()`。`email.header.decode_header()` 返回的字符集名称可能不是 Python 可识别的 codec；当前实现直接把该名称传给 `bytes.decode()`，因此 `errors="replace"` 也无法避免 `LookupError`。

影响：

- 单封邮件的未知字符集可能使整个 durable 轮询周期失败。
- 该邮件无法可靠完成 durable intake，也可能延迟后续邮件的接收和处理。
- 进程本身未退出，但只能等待下一轮轮询重试，不能视为成功处理。
- 真实邮件验证无法满足“无未处理轮询异常”的发布条件。

## 后续修复建议

1. 在 `_decode_str()` 中校验字符集名称，对未知或非法 codec 使用明确的安全 fallback，不能让 `LookupError` 冒出。
2. 增加针对 `decode_header()` 返回 `(bytes, "unknown-8bit")` 的单元测试，并覆盖多段主题拼接。
3. 增加消息级隔离测试：单封异常邮件不能中断同一轮中其他邮件的 durable intake。
4. 修复后重新验证任务状态、`\Seen` 时机、SMTP outbox 状态和重复投递行为，确认异常邮件不会造成重复回复或静默丢失。
5. 重新部署 PR #37 修复版本，至少观察一次完整的 IMAP 轮询周期；确认无该异常、下载完成、SMTP 回复成功且容器无重启。

## 回退验证记录

- 回退提交：`03d6d6e`
- `bot`、`file_browser`：均为 `running`
- 回退后容器重启次数：`0`
- 测试结果：图文下载成功，SMTP 回复成功
- PR #37：保持开放，未合并
