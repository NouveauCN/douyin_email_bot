import assert from "node:assert/strict";
import test from "node:test";
import { loadConfig } from "../src/config.js";

test("uses the internal bridge default and names the required token", () => {
  const names = ["QQBOT_APP_ID", "QQBOT_APP_SECRET", "QQ_BRIDGE_TOKEN", "QQ_BRIDGE_URL", "QQBOT_STATE_DIR"];
  const saved = Object.fromEntries(names.map((name) => [name, process.env[name]]));
  try {
    process.env.QQBOT_APP_ID = "app";
    process.env.QQBOT_APP_SECRET = "secret";
    process.env.QQ_BRIDGE_TOKEN = "token";
    delete process.env.QQ_BRIDGE_URL;
    delete process.env.QQBOT_STATE_DIR;
    const config = loadConfig();
    assert.equal(config.bridgeUrl, "http://bot:8082");
    delete process.env.QQ_BRIDGE_TOKEN;
    assert.throws(() => loadConfig(), /QQ_BRIDGE_TOKEN/);
  } finally {
    for (const name of names) {
      if (saved[name] === undefined) delete process.env[name];
      else process.env[name] = saved[name];
    }
  }
});
