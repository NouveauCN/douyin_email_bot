import path from "node:path";

function positiveInteger(name, value, fallback) {
  if (value === undefined || value === "") return fallback;
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) {
    throw new Error(`${name} must be a positive integer`);
  }
  return parsed;
}

function openIds(value) {
  const ids = new Set(String(value ?? "").split(",").map((id) => id.trim()).filter(Boolean));
  if (ids.has("*")) throw new Error("QQBOT_ALLOWED_OPENIDS must contain explicit OpenIDs");
  return ids;
}

export function loadConfig({ requireCredentials = true } = {}) {
  const appId = process.env.QQBOT_APP_ID?.trim() ?? "";
  const appSecret = process.env.QQBOT_APP_SECRET?.trim() ?? "";
  if (requireCredentials && (!appId || !appSecret)) {
    throw new Error("QQBOT_APP_ID and QQBOT_APP_SECRET are required");
  }
  const stateDir = path.resolve(process.env.QQBOT_STATE_DIR?.trim() || "/app/qq-gateway-state");
  const bridgeUrl = (process.env.QQ_BRIDGE_URL?.trim() || "http://bot:8082").replace(/\/+$/, "");
  const bridgeToken = process.env.QQ_BRIDGE_TOKEN?.trim() ?? "";
  if (requireCredentials && !bridgeToken) throw new Error("QQ_BRIDGE_TOKEN is required");
  return {
    appId,
    appSecret,
    allowedOpenIds: openIds(process.env.QQBOT_ALLOWED_OPENIDS),
    stateDir,
    bridgeUrl,
    bridgeToken,
    workerId: process.env.QQ_GATEWAY_WORKER_ID?.trim() || "qq-gateway",
    pollMs: positiveInteger("QQBOT_OUTBOX_POLL_MS", process.env.QQBOT_OUTBOX_POLL_MS, 1_000),
    claimSize: positiveInteger("QQBOT_OUTBOX_CLAIM_SIZE", process.env.QQBOT_OUTBOX_CLAIM_SIZE, 10),
    requestTimeoutMs: positiveInteger("QQBOT_REQUEST_TIMEOUT_MS", process.env.QQBOT_REQUEST_TIMEOUT_MS, 15_000),
  };
}
