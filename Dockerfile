# Arcanum: the API server with the web UI built in, as one image.

# ---- 1. Build the web UI -------------------------------------------------------------------------
FROM node:22.12-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
# Dependency install scripts are not needed: the bundler's native binary ships as a normal package.
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---- 2. Runtime ------------------------------------------------------------------------------------
FROM python:3.12-slim AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ARCANUM_HOST=0.0.0.0 \
    ARCANUM_WEB_DIR=/app/web/dist \
    ARCANUM_DATA_DIR=/tmp/arcanum-sessions

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY arcanum ./arcanum
RUN pip install ".[server]" && rm -rf /root/.cache
COPY --from=web /web/dist ./web/dist

# Run as an unprivileged user; session databases live in its temporary directory.
RUN useradd --create-home --uid 10001 arcanum
USER arcanum

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/health', timeout=4)"

CMD ["python", "-m", "arcanum.server"]
