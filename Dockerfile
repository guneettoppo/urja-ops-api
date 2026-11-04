# syntax=docker/dockerfile:1
FROM python:3.12-slim

# uv is used for dependency resolution; it is a single static binary.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    DATABASE_PATH=/data/urja.db

WORKDIR /app

# Dependencies first so the layer caches across source edits.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY urja ./urja
COPY scripts ./scripts

# The index is derived data: mount this as a volume to survive restarts.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# On first boot the index is empty, so the app syncs in the background and
# serves whatever is already indexed meanwhile.
CMD ["uv", "run", "uvicorn", "urja.main:app", "--host", "0.0.0.0", "--port", "8080"]
