"""Extract supported video URLs from text (email body, subject, etc.)."""

import re

# Match Douyin short links and full video/note URLs.  Short links are a single
# path token; email clients may append HTML entities like &nbsp; immediately
# after the copied URL.
DOUYIN_URL_PATTERN = re.compile(
    r"https?://(?:v\.douyin\.com/[A-Za-z0-9_-]+/?|www\.douyin\.com/(?:video|note)/\d+)"
)

# Match Bilibili video, bangumi, cheese, and short-share URLs.
BILIBILI_URL_PATTERN = re.compile(
    r"https?://(?:(?:www|m)\.)?bilibili\.com/\S+|https?://b23\.tv/\S+"
)

SUPPORTED_URL_PATTERN = re.compile(
    rf"{DOUYIN_URL_PATTERN.pattern}|{BILIBILI_URL_PATTERN.pattern}"
)


def clean_url(url: str) -> str:
    """Trim common punctuation copied around share URLs."""
    return url.rstrip(" \t\r\n>）)]}，。！？、；;,.!?")


def detect_platform(url: str) -> str | None:
    """Return the platform name for a supported URL."""
    if DOUYIN_URL_PATTERN.fullmatch(url):
        return "douyin"
    if BILIBILI_URL_PATTERN.fullmatch(url):
        return "bilibili"
    return None


class UrlExtractor:
    """Extract supported video URLs from plain text.

    Accepts any string input (email body, subject line, etc.) and
    returns the first matched supported URL, or None.
    """

    def extract(self, text: str) -> str | None:
        """Scan text for a supported URL and return the first match."""
        if not text:
            return None
        m = SUPPORTED_URL_PATTERN.search(text)
        return clean_url(m.group(0)) if m else None
