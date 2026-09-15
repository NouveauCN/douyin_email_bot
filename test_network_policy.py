"""Regressions for inherited proxies across the service stack."""

import asyncio
import os
from pathlib import Path

import httpx
import yaml

from network_policy import direct_environment, direct_firefox_options


def test_direct_environment_preserves_secrets_but_disables_proxy_variants():
    original = {"HTTPS_PROXY": "http://proxy.invalid:8888", "HtTp_PrOxY": "http://proxy.invalid",
                "npm_config_proxy": "http://proxy.invalid", "EMAIL_PASSWORD": "test-secret"}
    result = direct_environment(original)
    assert result["HTTPS_PROXY"] == result["HtTp_PrOxY"] == result["npm_config_proxy"] == ""
    assert result["NO_PROXY"] == result["no_proxy"] == "*"
    assert result["EMAIL_PASSWORD"] == "test-secret"
    assert original["HTTPS_PROXY"]


def test_f2_httpx_transport_stays_direct_after_inherited_proxy(monkeypatch):
    from f2_bootstrap import bootstrap_f2

    bootstrap_f2()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    bootstrap_f2()  # also enforces the policy on the idempotent startup path
    from f2.crawlers.base_crawler import BaseCrawler
    from f2.apps.douyin.utils import TokenManager

    async def check():
        async with BaseCrawler() as crawler:
            transport = crawler.aclient._transport_for_url(httpx.URL("https://www.douyin.com/"))
            assert type(transport._pool).__name__ == "AsyncConnectionPool"

    asyncio.run(check())
    assert TokenManager.proxies == {}


def test_firefox_overrides_saved_system_or_pac_proxy(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy.invalid:1080")
    options = direct_firefox_options()
    assert options["firefox_user_prefs"]["network.proxy.type"] == 0
    assert options["firefox_user_prefs"]["network.proxy.autoconfig_url"] == ""
    assert options["env"]["ALL_PROXY"] == ""
    assert os.environ["ALL_PROXY"]  # child environment does not modify caller


def test_every_compose_service_disables_runtime_and_build_proxies():
    config = yaml.safe_load(Path(__file__).with_name("docker-compose.yml").read_text())
    for service in config["services"].values():
        runtime = dict(item.split("=", 1) for item in service["environment"])
        for environment in (runtime, service["build"]["args"]):
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                assert environment[key] == ""
            assert environment["NO_PROXY"] == environment["no_proxy"] == "*"
