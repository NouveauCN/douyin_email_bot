"""WcfBot — WeChat message loop powered by WeChatFerry."""

import logging
import time
from queue import Empty

from wcferry import Wcf, WxMsg

from douyin_downloader import DouyinDownloader
from url_extractor import UrlExtractor

logger = logging.getLogger("WcfBot")


class WcfBot:
    """Listens for Douyin share links in WeChat and downloads the videos."""

    def __init__(self, config):
        """
        Args:
            config: AppConfig dataclass instance.
        """
        self.config = config

        logger.info("Connecting to WeChatFerry (waiting for WeChat login)...")
        self.wcf = Wcf(
            host=config.wechat.host,
            port=config.wechat.port,
            debug=config.wechat.debug,
            block=True,  # Wait for QR-code login before proceeding
        )

        self.downloader = DouyinDownloader(config.douyin)
        self.extractor = UrlExtractor()
        self._cooldowns: dict[str, float] = {}  # sender_wxid -> last_download_time

        logger.info("WcfBot initialized (self wxid: %s)", self.wcf.get_self_wxid())

    def run(self) -> None:
        """Start the message loop. Blocks until interrupted."""
        if not self.wcf.is_login():
            logger.error("WeChat is not logged in — cannot start message loop")
            return

        self.wcf.enable_receiving_msg()
        logger.info("Bot started. Waiting for messages...")

        while self.wcf.is_receiving_msg():
            try:
                msg = self.wcf.get_msg(block=True)
            except Empty:
                time.sleep(self.config.bot.message_delay)
                continue

            if msg is None:
                continue

            self._handle_message(msg)
            time.sleep(self.config.bot.message_delay)

    def _handle_message(self, msg: WxMsg) -> None:
        """Process a single incoming WeChat message."""
        # Ignore own messages, group messages, and empty content
        if msg.from_self():
            return
        if msg.from_group():
            return

        # Check sender allowlist (empty = allow all)
        allowed = self.config.bot.allowed_senders
        if allowed and msg.sender not in allowed:
            return

        url = self.extractor.extract(msg)
        if url is None:
            return  # Not a Douyin link — ignore silently

        sender = msg.sender
        logger.info("Douyin URL from %s: %s", sender, url)

        # Cooldown check
        now = time.time()
        if sender in self._cooldowns:
            elapsed = now - self._cooldowns[sender]
            if elapsed < self.config.bot.cooldown_seconds:
                remaining = int(self.config.bot.cooldown_seconds - elapsed)
                self._reply(sender, f"请等待 {remaining} 秒后再发送链接")
                return

        # Acknowledge receipt
        self._reply(sender, "正在下载抖音视频，请稍候...")

        # Download
        result = self.downloader.download(url)

        if result["success"]:
            self._cooldowns[sender] = time.time()
            filepath = result["filepath"] or "未知路径"
            self._reply(
                sender,
                f"下载完成！\n"
                f"标题：{result['title']}\n"
                f"保存位置：{filepath}",
            )
        else:
            self._reply(sender, f"下载失败：{result['error']}")

    def _reply(self, sender: str, text: str) -> None:
        """Send a text reply and log any failures."""
        ret = self.wcf.send_text(text, sender)
        if ret != 0:
            logger.error("Failed to send text to %s (code %d)", sender, ret)

    def cleanup(self) -> None:
        """Release WeChatFerry resources."""
        self.wcf.cleanup()
        logger.info("WcfBot cleaned up")
