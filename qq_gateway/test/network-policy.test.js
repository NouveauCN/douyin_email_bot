import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";

test("gateway startup disables inherited SDK and environment proxies", () => {
  const moduleUrl = new URL("../src/network-policy.js", import.meta.url).href;
  const output = execFileSync(process.execPath, ["--input-type=module", "-e", `
    await import(${JSON.stringify(moduleUrl)});
    for (const key of ['HTTP_PROXY','https_proxy','GLOBAL_AGENT_HTTP_PROXY','npm_config_proxy']) {
      if (process.env[key]) throw new Error('proxy remained configured');
    }
    if (process.env.NO_PROXY !== '*' || process.env.NODE_USE_ENV_PROXY !== '0') throw new Error('bypass missing');
    console.log('direct');
  `], { env: { ...process.env, HTTP_PROXY: "http://proxy.invalid", https_proxy: "http://proxy.invalid",
    GLOBAL_AGENT_HTTP_PROXY: "http://proxy.invalid", npm_config_proxy: "http://proxy.invalid" } });
  assert.equal(output.toString().trim(), "direct");
});
