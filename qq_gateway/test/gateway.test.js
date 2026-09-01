import assert from "node:assert/strict";
import test from "node:test";
import { handleInboundMessage, helpText, parseCommand } from "../src/gateway.js";

function setup(allowed = ["allowed"]) {
  const sent = [];
  const bot = { send: async (message) => { sent.push(message); return {}; } };
  const bridgeCalls = [];
  const bridge = { enqueueMessage: async (message) => { bridgeCalls.push(message); return { accepted: true }; } };
  const config = { allowedOpenIds: new Set(allowed), bridgeToken: "secret" };
  return { bot, bridge, config, sent, bridgeCalls };
}

const message = (content, senderId = "allowed") => ({
  kind: "c2c", senderId, messageId: "inbound-1", timestamp: "2026-09-01T00:00:00Z", content,
  replyTarget: { scope: "c2c", targetId: senderId, msgId: "inbound-1" },
});

test("supports OpenID and help commands before allowlist", async () => {
  assert.equal(parseCommand("/openid"), "openid");
  assert.equal(parseCommand("/帮助"), "help");
  const ctx = setup([]);
  await handleInboundMessage({ ...ctx, message: message("/openid", "unknown") });
  await handleInboundMessage({ ...ctx, message: message("/帮助", "unknown") });
  assert.equal(ctx.sent.length, 2);
  assert.equal(ctx.sent[0].msgType, 0);
  assert.match(ctx.sent[0].content, /unknown/);
  assert.match(ctx.sent[1].content, /每条消息只能包含一个链接/);
});

test("allows exactly one URL and rejects no/multiple URLs", async () => {
  const ctx = setup();
  await handleInboundMessage({ ...ctx, message: message("下载 https://v.douyin.com/a") });
  assert.equal(ctx.bridgeCalls.length, 1);
  assert.equal(ctx.bridgeCalls[0].url, "https://v.douyin.com/a");
  await handleInboundMessage({ ...ctx, message: message("https://v.douyin.com/a https://b23.tv/b") });
  await handleInboundMessage({ ...ctx, message: message("hello") });
  assert.equal(ctx.bridgeCalls.length, 1);
  assert.match(ctx.sent.at(-2).content, /只能包含一个/);
  assert.match(ctx.sent.at(-1).content, /没有找到/);
});

test("does not expose bridge token in enqueue failure reply", async () => {
  const ctx = setup();
  ctx.bridge.enqueueMessage = async () => { throw new Error(`bad token ${ctx.config.bridgeToken}`); };
  const result = await handleInboundMessage({ ...ctx, message: message("https://v.douyin.com/a") });
  assert.equal(result.kind, "error");
  assert.doesNotMatch(ctx.sent[0].content, /secret/);
});

test("ignores non-C2C messages", async () => {
  const ctx = setup();
  assert.deepEqual(await handleInboundMessage({ ...ctx, message: { kind: "group", senderId: "allowed" } }), { handled: false });
  assert.equal(ctx.sent.length, 0);
});

assert.match(helpText(), /QQ 私聊/);
