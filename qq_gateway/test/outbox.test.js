import assert from "node:assert/strict";
import test from "node:test";
import { sendOutboxItem } from "../src/outbox.js";

test("sends an outbox reply with stable msg_seq and acknowledges it", async () => {
  const sent = [];
  const acks = [];
  const bot = { send: async (message) => { sent.push(message); } };
  const bridge = {
    ackOutbox: async (...args) => acks.push(args),
    failOutbox: async () => assert.fail("must not fail a successful reply"),
  };
  const item = { id: "out-1", openid: "user", message_id: "in-1", lease_token: "lease", content: "已接收", msg_seq: 77 };
  assert.deepEqual(await sendOutboxItem({ bot, bridge, item }), { status: "sent", id: "out-1" });
  assert.equal(sent[0].extra.msg_seq, 77);
  assert.equal(sent[0].msgType, 0);
  assert.deepEqual(sent[0].target, { scope: "c2c", targetId: "user", msgId: "in-1" });
  assert.deepEqual(acks, [["out-1", "lease"]]);
});

test("does not send an expired passive reply", async () => {
  let sent = false;
  let failure;
  const bridge = { ackOutbox: async () => {}, failOutbox: async (...args) => { failure = args; } };
  const result = await sendOutboxItem({
    bot: { send: async () => { sent = true; } }, bridge,
    item: { id: "out-2", open_id: "user", message_id: "in-2", lease_token: "lease", content: "late", expires_at: "2020-01-01T00:00:00Z" },
  });
  assert.equal(result.status, "expired");
  assert.equal(sent, false);
  assert.deepEqual(failure, ["out-2", "lease", "reply window expired", false]);
});
