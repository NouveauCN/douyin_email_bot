import http from "node:http";
import https from "node:https";

export class BridgeError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = "BridgeError";
    this.status = status;
  }
}

export class BridgeClient {
  constructor({ baseUrl, token, timeoutMs = 15_000, request = requestJson } = {}) {
    this.baseUrl = String(baseUrl ?? "").replace(/\/+$/, "");
    this.token = token ?? "";
    if (!this.baseUrl) throw new Error("bridge URL is required");
    if (!this.token) throw new Error("bridge token is required");
    this.timeoutMs = timeoutMs;
    this.request = request;
  }

  async post(path, body) {
    return this.request(new URL(path, `${this.baseUrl}/`).toString(), {
      method: "POST",
      timeoutMs: this.timeoutMs,
      token: this.token,
      body,
    });
  }

  enqueueMessage({ openId, messageId, timestamp, content, url }) {
    return this.post("/v1/qq/messages", {
      source: "qq",
      open_id: openId,
      message_id: messageId,
      timestamp: timestamp ?? null,
      content,
      text: content,
      url,
    });
  }

  async claimOutbox({ limit = 10, workerId = "qq-gateway" } = {}) {
    const result = await this.post("/v1/qq/outbox/claim", {
      consumer: "qq",
      limit,
      worker_id: workerId,
    });
    if (Array.isArray(result)) return result;
    if (Array.isArray(result?.items)) return result.items;
    if (Array.isArray(result?.outbox)) return result.outbox;
    if (result?.item) return [result.item];
    return [];
  }

  ackOutbox(id, leaseToken) {
    return this.post(`/v1/qq/outbox/${encodeURIComponent(id)}/ack`, { lease_token: leaseToken });
  }

  failOutbox(id, leaseToken, error, retryable = true) {
    return this.post(`/v1/qq/outbox/${encodeURIComponent(id)}/fail`, {
      lease_token: leaseToken,
      error: String(error).slice(0, 240),
      retryable: Boolean(retryable),
    });
  }
}

async function requestJson(url, { method, timeoutMs, token, body }) {
  const target = new URL(url);
  const transport = target.protocol === "https:" ? https : http;
  const payload = JSON.stringify(body ?? {});
  return new Promise((resolve, reject) => {
    const req = transport.request(target, {
      method,
      timeout: timeoutMs,
      headers: {
        accept: "application/json",
        "content-type": "application/json",
        "content-length": Buffer.byteLength(payload),
        ...(token ? { authorization: `Bearer ${token}` } : {}),
      },
    }, (res) => {
      const chunks = [];
      res.setEncoding("utf8");
      res.on("data", (chunk) => chunks.push(chunk));
      res.on("end", () => {
        const text = chunks.join("");
        let data = {};
        if (text) {
          try { data = JSON.parse(text); } catch { reject(new BridgeError("bridge returned invalid JSON", res.statusCode)); return; }
        }
        if ((res.statusCode ?? 500) < 200 || (res.statusCode ?? 500) >= 300) {
          reject(new BridgeError(`bridge HTTP ${res.statusCode}`, res.statusCode));
          return;
        }
        resolve(data);
      });
    });
    req.on("timeout", () => req.destroy(new BridgeError("bridge request timed out")));
    req.on("error", reject);
    req.end(payload);
  });
}
