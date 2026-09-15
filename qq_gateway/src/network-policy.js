// Evaluate before loading the QQ SDK or creating HTTP/WebSocket clients.
const proxyNames = new Set([
  "http_proxy", "https_proxy", "all_proxy", "ftp_proxy", "socks_proxy",
  "ws_proxy", "wss_proxy", "npm_config_proxy", "npm_config_http_proxy",
  "npm_config_https_proxy", "global_agent_http_proxy", "global_agent_https_proxy",
]);
for (const key of Object.keys(process.env)) {
  if (proxyNames.has(key.toLowerCase())) process.env[key] = "";
}
for (const key of proxyNames) {
  process.env[key] = "";
  process.env[key.toUpperCase()] = "";
}
process.env.NO_PROXY = "*";
process.env.no_proxy = "*";
process.env.NODE_USE_ENV_PROXY = "0";
process.env.GLOBAL_AGENT_NO_PROXY = "*";
