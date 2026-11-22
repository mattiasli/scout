# Scout server: one process for the website, the MCP endpoint and the car's WebSocket.
# Build from the server/ folder (Railway: root directory /server).

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /usr/local/bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first, so a code change doesn't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# The server runs as this unprivileged user. Railway mounts the volume at /data owned by
# root, so the container starts as root and scout-server itself takes ownership of the
# data directory and switches to SCOUT_RUN_AS before opening the database or any socket
# (see drop_root in src/scout/__main__.py).
RUN useradd --system --user-group --uid 10001 --home-dir /app --no-create-home --shell /usr/sbin/nologin scout

# The data directory is SCOUT_DATA_DIR, else Railway's RAILWAY_VOLUME_MOUNT_PATH, else ./data.
ENV PATH="/app/.venv/bin:$PATH" \
    SCOUT_RUN_AS=scout \
    PORT=8000

EXPOSE 8000

CMD ["scout-server"]
