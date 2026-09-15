"""Direct-only networking for the service stack and its child processes."""

import os
from collections.abc import Mapping


_PROXY_KEYS = frozenset({
    "http_proxy", "https_proxy", "all_proxy", "ftp_proxy", "socks_proxy",
    "ws_proxy", "wss_proxy", "npm_config_proxy", "npm_config_http_proxy",
    "npm_config_https_proxy", "global_agent_http_proxy", "global_agent_https_proxy",
})


def direct_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Keep unrelated settings while disabling inherited proxy configuration."""
    result = dict(os.environ if environment is None else environment)
    for key in list(result):
        if key.lower() in _PROXY_KEYS:
            result[key] = ""
    for key in _PROXY_KEYS:
        result[key] = ""
        result[key.upper()] = ""
    result.update(NO_PROXY="*", no_proxy="*", NODE_USE_ENV_PROXY="0",
                  GLOBAL_AGENT_NO_PROXY="*")
    return result


def enforce_direct_network() -> None:
    """Apply before F2 imports, including when running outside Docker."""
    os.environ.update(direct_environment())


def direct_firefox_options() -> dict:
    """Override saved Firefox system/PAC/manual proxy preferences explicitly."""
    return {
        "env": direct_environment(),
        "firefox_user_prefs": {
            "network.proxy.type": 0,
            "network.proxy.autoconfig_url": "",
        },
    }
