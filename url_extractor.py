"""Extract Douyin URLs from text (email body, subject, etc.)."""

import re

# Match Douyin short links and full video/note URLs
DOUYIN_URL_PATTERN = re.compile(
    r"https?://(?:v\.douyin\.com/\S+|www\.douyin\.com/(?:video|note)/\d+)"
)


class UrlExtractor:
    """Extract Douyin video URLs from plain text.

    Accepts any string input (email body, subject line, etc.) and
    returns the first matched Douyin URL, or None.
    """

    def extract(self, text: str) -> str | None:
        """Scan text for a Douyin URL and return the first match."""
        if not text:
            return None
        m = DOUYIN_URL_PATTERN.search(text)
        return m.group(0) if m else None
