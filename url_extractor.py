"""Extract Douyin URLs from WeChat messages."""

import re
import xml.etree.ElementTree as ET

from wcferry import WxMsg

# Match Douyin short links and full video/note URLs
DOUYIN_URL_PATTERN = re.compile(
    r"https?://(?:v\.douyin\.com/\S+|www\.douyin\.com/(?:video|note)/\d+)"
)


class UrlExtractor:
    """Extract Douyin video URLs from incoming WeChat messages.

    Handles two message types:
    - Type 49: link/card messages (parse XML body for URL)
    - Type 1: text messages (regex scan for Douyin URLs)
    """

    def extract(self, msg: WxMsg) -> str | None:
        """Try to extract a Douyin URL from a WeChat message.

        Returns the first matched URL, or None if no Douyin URL found.
        """
        # Path 1: Type 49 — link / card message (shared content)
        if msg.type == 49 and msg.xml:
            url = self._from_xml(msg.xml)
            if url:
                return url

        # Path 2: Text message
        if msg.is_text() and msg.content:
            url = self._from_text(msg.content)
            if url:
                return url

        return None

    def _from_xml(self, xml_str: str) -> str | None:
        """Parse WeChat XML card message and search for Douyin URL."""
        try:
            root = ET.fromstring(xml_str)
            for elem in root.iter():
                if elem.text and DOUYIN_URL_PATTERN.search(elem.text):
                    return DOUYIN_URL_PATTERN.search(elem.text).group(0)
        except ET.ParseError:
            pass
        return None

    def _from_text(self, content: str) -> str | None:
        """Scan plain text for Douyin URL patterns."""
        m = DOUYIN_URL_PATTERN.search(content)
        return m.group(0) if m else None
