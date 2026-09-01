import assert from "node:assert/strict";
import test from "node:test";
import { BridgeClient } from "../src/bridge.js";

test("uses the authenticated bridge contract and bounded retry fields", async () => {
  const calls = [];
  const bridge = new BridgeClient({
    baseUrl: "http://bot:8090",
    token: "bridge-secret",
    request: async (url, options) => { calls.push({ url, options }); return { items: [] }; },
  });
  await bridge.enqueueMessage({ openId: "u", messageId: "m", content: "url", url: "https://v.douyin.com/a" });
  await bridge.claimOutbox({ limit: 2, workerId: "qq-gateway" });
  await bridge.failOutbox("4", "lease", "network", true);
  assert.equal(calls[0].url, "http://bot:8090/v1/qq/messages");
  assert.equal(calls[0].options.body.open_id, "u");
  assert.equal(calls[1].options.body.worker_id, "qq-gateway");
  assert.equal(calls[1].options.body.lease_seconds, undefined);
  assert.deepEqual(calls[2].options.body, { lease_token: "lease", error: "network", retryable: true });
});

test("requires a bridge token", () => {
  assert.throws(() => new BridgeClient({ baseUrl: "http://bot:8090" }), /token is required/);
});
