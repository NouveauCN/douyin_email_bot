export function safeError(error, secret = "") {
  let message = error instanceof Error ? error.message : String(error);
  if (secret) message = message.split(secret).join("[redacted]");
  message = message.replace(/https?:\/\/\S+/giu, "[url]");
  return message.replace(/[\r\n\t]+/gu, " ").slice(0, 240);
}

export function logError(prefix, error, secret = "") {
  console.error(`${prefix}: ${safeError(error, secret)}`);
}

export function messageSequence(key) {
  let hash = 2_166_136_261;
  for (const character of String(key ?? "")) {
    hash ^= character.codePointAt(0);
    hash = Math.imul(hash, 16_777_619);
  }
  // Match the SDK's own 16-bit sequence range while keeping retries stable.
  return (hash >>> 0) % 65_535 + 1;
}
