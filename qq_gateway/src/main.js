#!/usr/bin/env node
import { BridgeClient } from "./bridge.js";
import { loadConfig } from "./config.js";
import { createGateway } from "./gateway.js";
import { handleInboundMessage } from "./gateway.js";
import { logError } from "./logging.js";
import { OutboxPump } from "./outbox.js";
import { mkdir } from "node:fs/promises";

process.umask(0o077);

export async function main() {
  const config = loadConfig();
  await mkdir(config.stateDir, { recursive: true, mode: 0o700 });
  const bridge = new BridgeClient({ baseUrl: config.bridgeUrl, token: config.bridgeToken, timeoutMs: config.requestTimeoutMs });
  const { bot, sessionStore } = createGateway({ config });
  const pump = new OutboxPump({
    bridge,
    bot,
    pollMs: config.pollMs,
    claimSize: config.claimSize,
    workerId: config.workerId,
  });
  bot.on("ready", () => {
    console.info("[qq-gateway] connected to QQ Open Platform");
    pump.start();
  });
  bot.on("resumed", () => console.info("[qq-gateway] session resumed"));
  bot.on("error", (error) => logError("[qq-gateway] SDK error", error, config.appSecret));
  bot.on("message", (_context, message) => handleInboundMessage({ message, config, bridge, bot }).catch((error) => logError("[qq-gateway] message handler failed", error, config.appSecret)));
  let stopping = false;
  const stop = (signal) => {
    if (stopping) return;
    stopping = true;
    console.info(`[qq-gateway] stopping after ${signal}`);
    pump.stop();
    bot.stop();
    sessionStore.flush?.();
  };
  process.once("SIGINT", () => stop("SIGINT"));
  process.once("SIGTERM", () => stop("SIGTERM"));
  try {
    await bot.start();
  } finally {
    stop("gateway exit");
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    logError("[qq-gateway] fatal", error, process.env.QQBOT_APP_SECRET);
    process.exitCode = 1;
  });
}
