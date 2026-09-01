import { logError, messageSequence, safeError } from "./logging.js";

function value(item, ...names) {
  for (const name of names) {
    if (item?.[name] !== undefined && item?.[name] !== null) return item[name];
  }
  return undefined;
}

export function normalizeOutboxItem(item) {
  const id = value(item, "id", "outbox_id", "outboxId");
  const openId = value(item, "openid", "open_id", "openId", "target_openid", "targetOpenId");
  const messageId = value(item, "message_id", "messageId", "reply_message_id", "replyMessageId", "in_reply_to");
  const leaseToken = value(item, "lease_token", "leaseToken", "lease");
  const rawSequence = value(item, "msg_seq", "msgSeq", "sequence");
  const payload = value(item, "payload");
  const msgSeq = Number.isSafeInteger(Number(rawSequence)) && Number(rawSequence) > 0
    ? Number(rawSequence)
    : messageSequence(`${id ?? "outbox"}:reply`);
  let content = value(item, "content", "text", "body");
  if ((content === undefined || content === null || typeof content !== "string") && payload && typeof payload === "object") {
    content = payload.body ?? payload.content ?? payload.text ?? payload.message;
    if (typeof content !== "string") {
      const event = String(value(item, "event") ?? payload.event ?? "");
      if (event === "accepted") content = "已收到链接，开始下载。";
      else if (event === "completed") content = formatCompleted(payload);
      else if (event === "partially_completed") content = formatPartial(payload);
      else if (event === "failed") content = String(payload.error || "下载失败，请稍后重试。");
    }
  }
  return {
    raw: item,
    id: id == null ? "" : String(id),
    openId: openId == null ? "" : String(openId),
    messageId: messageId == null ? "" : String(messageId),
    leaseToken: leaseToken == null ? "" : String(leaseToken),
    content: String(content ?? ""),
    msgSeq,
    expiresAt: value(item, "expires_at", "expiresAt", "reply_expires_at", "replyExpiresAt"),
  };
}

function formatCompleted(payload) {
  const result = payload.result;
  if (result && typeof result === "object") {
    const files = Array.isArray(result.files) ? result.files : [];
    if (files.length) return `下载完成，共 ${files.length} 个文件：\n${files.map((file) => String(file).split(/[\\/]/u).pop()).join("\n")}`;
    if (Number.isFinite(result.file_count)) return `下载完成，共 ${result.file_count} 个文件。`;
  }
  return "下载完成。";
}

function formatPartial(payload) {
  const result = payload.result;
  if (result && typeof result === "object") {
    const succeeded = result.files_succeeded ?? result.success_count;
    const failed = result.files_failed ?? result.failure_count;
    if (succeeded !== undefined || failed !== undefined) return `下载部分完成：成功 ${succeeded ?? 0} 个，失败 ${failed ?? 0} 个。`;
  }
  return "下载部分完成，部分文件失败。";
}

export async function sendOutboxItem({ bot, bridge, item, now = Date.now() }) {
  const normalized = normalizeOutboxItem(item);
  if (!normalized.id || !normalized.openId || !normalized.messageId || !normalized.leaseToken) {
    throw new Error("outbox item is missing required fields");
  }
  const expiry = typeof normalized.expiresAt === "number"
    ? normalized.expiresAt * 1000
    : Date.parse(normalized.expiresAt);
  if (normalized.expiresAt && Number.isFinite(expiry) && expiry <= now) {
    await bridge.failOutbox(normalized.id, normalized.leaseToken, "reply window expired", false);
    return { status: "expired", id: normalized.id };
  }
  if (!normalized.content.trim()) {
    await bridge.failOutbox(normalized.id, normalized.leaseToken, "empty reply", false);
    return { status: "failed", id: normalized.id };
  }
  try {
    await bot.send({
      target: { scope: "c2c", targetId: normalized.openId, msgId: normalized.messageId },
      msgType: 0,
      content: normalized.content,
      extra: { msg_seq: normalized.msgSeq },
    });
    await bridge.ackOutbox(normalized.id, normalized.leaseToken);
    return { status: "sent", id: normalized.id };
  } catch (error) {
    try {
      await bridge.failOutbox(normalized.id, normalized.leaseToken, safeError(error), true);
    } catch (failError) {
      logError(`[qq-gateway] failed to record outbox failure ${normalized.id}`, failError, bridge.token);
    }
    return { status: "failed", id: normalized.id, error };
  }
}

export class OutboxPump {
  constructor({ bridge, bot, pollMs = 1_000, claimSize = 10, workerId = "qq-gateway", logger = console } = {}) {
    this.bridge = bridge;
    this.bot = bot;
    this.pollMs = pollMs;
    this.claimSize = claimSize;
    this.workerId = workerId;
    this.logger = logger;
    this.timer = null;
    this.running = false;
    this.polling = false;
  }

  async poll() {
    if (this.polling || !this.running) return;
    this.polling = true;
    try {
      const items = await this.bridge.claimOutbox({ limit: this.claimSize, workerId: this.workerId });
      for (const item of items) await sendOutboxItem({ bot: this.bot, bridge: this.bridge, item });
    } catch (error) {
      this.logger.error?.(`[qq-gateway] outbox poll failed: ${safeError(error)}`);
    } finally {
      this.polling = false;
    }
  }

  start() {
    if (this.running) return;
    this.running = true;
    void this.poll();
    this.timer = setInterval(() => void this.poll(), this.pollMs);
    this.timer.unref?.();
  }

  stop() {
    this.running = false;
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }
}
