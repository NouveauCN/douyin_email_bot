FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ── System deps ────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Convenience: python3 -> python
RUN ln -sf /usr/bin/python3 /usr/bin/python

WORKDIR /app

# ── Python deps (layer cache: rarely changes) ──────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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
