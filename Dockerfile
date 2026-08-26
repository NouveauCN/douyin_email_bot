FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ── System deps + ffmpeg ───────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# ── Python deps (frozen from the authoritative uv lock) ────────────
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# yutto conflicts with F2's pinned aiofiles/pydantic dependency set, so install
# its separate frozen lock into an isolated environment and expose only the CLI.
COPY dependency-locks/yutto/pyproject.toml dependency-locks/yutto/uv.lock /tmp/yutto-lock/
RUN UV_PROJECT_ENVIRONMENT=/opt/yutto uv sync \
        --project /tmp/yutto-lock --frozen --no-dev --no-install-project \
    && ln -s /opt/yutto/bin/yutto /usr/local/bin/yutto \
    && yutto --help >/dev/null

# ── Playwright Firefox (browser binary + system libs) ──────────────
RUN python3 -m playwright install-deps firefox
RUN python3 -m playwright install firefox

# ── Application code ───────────────────────────────────────────────
COPY . .

# Ensure volume mount points exist
RUN mkdir -p /app/downloads /app/logs /app/firefox_profile /app/conf

# ── Runtime ────────────────────────────────────────────────────────
# Bot entrypoint (web_login overrides via docker-compose command)
CMD ["python3", "main.py"]
