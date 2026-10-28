FROM python:3.12-slim

# Build proxies are cleared by the shared compose direct-network build args
# (the root Dockerfile relies on Docker's predefined proxy build args); pin
# the same direct-only policy here for local builds and for runtime.
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG ALL_PROXY
ARG NO_PROXY
ENV HTTP_PROXY="" \
    HTTPS_PROXY="" \
    ALL_PROXY="" \
    FTP_PROXY="" \
    SOCKS_PROXY="" \
    http_proxy="" \
    https_proxy="" \
    all_proxy="" \
    ftp_proxy="" \
    socks_proxy="" \
    NO_PROXY="*" \
    no_proxy="*"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Locked CPU-only dependencies (torch/torchvision resolve to +cpu wheels from
# the explicit PyTorch CPU index declared in pyproject.toml). --no-cache keeps
# the downloaded wheels out of the image layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

# Only this project's modules plus the closed tag vocabulary data file;
# the build context is ./tag_worker, so .probe and the rest of the
# repository never enter this image. vocabulary.json must ship with the
# code because normalize.py resolves it beside itself.
COPY *.py vocabulary.json ./tag_worker/

ENTRYPOINT ["python", "-m", "tag_worker.worker"]
