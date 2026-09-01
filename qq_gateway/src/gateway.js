import { accessPolicy, concurrencyGuard, FileKVStore, kvSessionPersistence, messageFilter, QQBot } from "@tencent-connect/qqbot-nodejs";
import { mkdir } from "node:fs/promises";
import { messageSequence, logError, safeError } from "./logging.js";
import { extractSupportedUrls } from "./urls.js";

export function parseCommand(content) {
  const value = String(content ?? "").trim();
  if (/^\/(?:openid|me)$/iu.test(value)) return "openid";
  if (/^\/(?:help|帮助)$/iu.test(value)) return "help";
  return value.startsWith("/") ? "unknown" : null;
}

export function helpText() {
  return [
    "抖音/B站下载机器人，仅支持 QQ 私聊。",
    "直接发送一条抖音或 B 站链接即可下载。",
    "每条消息只能包含一个链接。",
    "/openid：查看当前 QQ OpenID",
    "/帮助：显示本说明",
  ].join("\n");
}

function replyTarget(message) {
  return message?.replyTarget ?? {
    scope: "c2c",
    targetId: message.senderId,
    msgId: message.messageId,
  };
}

export function createGateway({ config, sessionStore, botFactory = (options) => new QQBot(options) } = {}) {
  if (!config) throw new Error("config is required");
  const store = sessionStore ?? new FileKVStore({
    dir: config.stateDir,
    fileName: "gateway.json",
    saveThrottleMs: 250,
    logger: { error: (message) => console.error(`[qq-gateway] session store: ${message}`) },
  });
  const bot = botFactory({
    appId: config.appId,
    appSecret: config.appSecret,
    accountId: config.appId,
    sessionPersistence: kvSessionPersistence({ store, accountId: config.appId }),
    intents: 1 << 25,
    logger: {
      debug: () => {},
      info: (message) => console.info(`[qq-gateway] ${safeError(message, config.appSecret)}`),
      warn: (message) => console.warn(`[qq-gateway] ${safeError(message, config.appSecret)}`),
      error: (message) => console.error(`[qq-gateway] ${safeError(message, config.appSecret)}`),
    },
    // Only C2C intents are needed. The SDK's bit mask is intentionally left
    // at its default because it varies between SDK releases; middleware below
    // still rejects every non-C2C event.
  });
  bot.use(
    messageFilter({ skipSelfEcho: true, dedup: { windowMs: 60_000, maxSize: 5_000 } }),
    concurrencyGuard({ strategy: "queue", maxQueue: 32, maxProcessingMs: 30_000 }),
    accessPolicy({
      c2c: { mode: "open" },
      group: { mode: "disabled" },
      guild: { mode: "disabled" },
      onBlock: (_ctx, reason) => console.warn(`[qq-gateway] blocked non-C2C event: ${reason}`),
    }),
  );
  return { bot, sessionStore: store };
}

export async function handleInboundMessage({ message, config, bridge, bot }) {
  if (!message || message.kind !== "c2c" || !message.senderId || !message.messageId) return { handled: false };
  const target = replyTarget(message);
  const sendReply = (content, suffix = "reply") => bot.send({
    target,
    msgType: 0,
    content,
    extra: { msg_seq: messageSequence(`${message.messageId}:${suffix}`) },
  });
  const command = parseCommand(message.content);
  if (command === "openid") {
    await sendReply(`你的 QQ OpenID：\n${message.senderId}`, "openid");
    return { handled: true, kind: command };
  }
  if (command === "help") {
    await sendReply(helpText(), "help");
    return { handled: true, kind: command };
  }
  if (!config.allowedOpenIds.has(message.senderId)) {
    await sendReply("当前用户不在下载白名单中。发送 /openid 获取 OpenID，然后在服务器配置 QQBOT_ALLOWED_OPENIDS。", "denied");
    return { handled: true, kind: "denied" };
  }
  if (command === "unknown") {
    await sendReply("未知命令。发送 /帮助 查看可用命令。", "unknown");
    return { handled: true, kind: command };
  }
  const urls = extractSupportedUrls(message.content);
  if (urls.length === 0) {
    await sendReply("没有找到抖音或 B 站链接。每条消息请直接发送一条支持的链接。", "no-link");
    return { handled: true, kind: "no-link" };
  }
  if (urls.length > 1) {
    await sendReply("一条消息只能包含一个抖音或 B 站链接，请拆成多条消息发送。", "many-links");
    return { handled: true, kind: "many-links" };
  }
  try {
    const result = await bridge.enqueueMessage({
      openId: message.senderId,
      messageId: message.messageId,
      timestamp: message.timestamp,
      content: message.content,
      url: urls[0],
    });
    if (result?.accepted === false) {
      await sendReply(String(result.reply || "链接未被接受，请检查后重试。"), "bridge-rejected");
      return { handled: true, kind: "rejected", result };
    }
    return { handled: true, kind: "queued", result };
  } catch (error) {
    logError("[qq-gateway] bridge enqueue failed", error, config.bridgeToken);
    await sendReply("提交下载任务失败，请稍后重试。", "bridge-error");
    return { handled: true, kind: "error" };
  }
}

export async function startGateway({ config, bridge }) {
  await mkdir(config.stateDir, { recursive: true, mode: 0o700 });
  const gateway = createGateway({ config });
  gateway.bot.on("ready", () => {
    console.info("[qq-gateway] connected to QQ Open Platform");
    if (config.allowedOpenIds.size === 0) console.warn("[qq-gateway] QQBOT_ALLOWED_OPENIDS is empty");
  });
  gateway.bot.on("resumed", () => console.info("[qq-gateway] session resumed"));
  gateway.bot.on("error", (error) => logError("[qq-gateway] SDK error", error, config.appSecret));
  gateway.bot.on("message", (_context, message) => handleInboundMessage({ message, config, bridge, bot: gateway.bot }).catch((error) => logError("[qq-gateway] message handler failed", error, config.appSecret)));
  await gateway.bot.start();
  return gateway;
}
