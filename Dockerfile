# Notary — single-image deployment.
#
# Builds the React frontend, then serves it from the same FastAPI process that
# serves the API. One container, one port, no reverse proxy to configure and no
# CORS to get wrong in production.
#
# Defaults to replay mode so the image runs with no credentials at all: a judge
# who clones this and runs `docker run -p 8000:8000 notary` gets a working app
# with seeded review recordings. Supply B2 credentials to additionally serve
# real certificates from a real Object-Locked vault.

# --------------------------------------------------------------- frontend
FROM node:22-alpine AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------- backend
FROM python:3.11-slim

# ffmpeg is required for keyframe extraction. Without it the Board cannot see
# the video, every visual criterion reports UNCERTAIN, and everything escalates
# to a human -- degraded but never silently passing.
RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/pyproject.toml backend/
COPY backend/notary backend/notary
RUN pip install --no-cache-dir ./backend

COPY scripts/ scripts/
COPY seed/ seed/
COPY docs/evaluation-report.json docs/evaluation-report.json
COPY --from=frontend /build/dist /app/static

# Run as a non-root user. The signing key lives on disk, so a container
# compromise should not also be a root compromise.
RUN useradd --create-home --uid 10001 notary \
    && mkdir -p /app/keys \
    && chown -R notary:notary /app
USER notary

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NOTARY_MODE=replay \
    NOTARY_SEED_DIR=/app/seed \
    NOTARY_SIGNING_KEY_PATH=/app/keys/notary-ed25519.pem \
    PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"

# Single worker on purpose. The SSE event bus is in-process, so a second worker
# would serve clients that never receive events from runs on the first. See
# docs/OPERATIONS.md#scaling for the Redis path.
CMD ["sh", "-c", "uvicorn notary.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 650"]
