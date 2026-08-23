FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ── System deps + ffmpeg ───────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Codex is used by the bot's failure hook. Authentication is kept in the
# compose-managed Codex home volume and is not baked into the image.
RUN curl -fsSL https://chatgpt.com/codex/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"
RUN codex --version >/dev/null
# Keep the persistent auth/config home separate from the installer-managed
# package directory under /root/.codex, so the compose volume cannot hide the
# installed executable.
ENV CODEX_HOME="/root/codex-home"

WORKDIR /app

# ── Python deps (layer cache: rarely changes) ──────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# yutto conflicts with F2's pinned aiofiles/pydantic dependency set, so keep it
# in an isolated CLI environment and expose only the executable to the bot.
RUN python3 -m venv /opt/yutto \
    && /opt/yutto/bin/pip install --no-cache-dir "yutto>=2.2.0" \
    && ln -s /opt/yutto/bin/yutto /usr/local/bin/yutto \
    && yutto --help >/dev/null

# ── Playwright Firefox (browser binary + system libs) ──────────────
RUN python3 -m playwright install-deps firefox
RUN python3 -m playwright install firefox

# ── Application code ───────────────────────────────────────────────
COPY . .

# Ensure volume mount points exist
RUN mkdir -p /app/downloads /app/logs /app/firefox_profile /app/conf /root/codex-home

# ── Runtime ────────────────────────────────────────────────────────
# Bot entrypoint (web_login overrides via docker-compose command)
CMD ["python3", "main.py"]
