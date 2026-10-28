# OwoBook web server (FastAPI + uvicorn). ffmpeg comes from the imageio-ffmpeg wheel, so no apt packages.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY alembic ./alembic
COPY owobook ./owobook
COPY scripts ./scripts
RUN uv sync --frozen --no-dev

RUN useradd --create-home owobook && chown -R owobook /app
USER owobook

# Render sets $PORT. One worker: per-trader locks and the reminder lock live in this process.
# Migrations run at startup (see main.py lifespan).
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn owobook.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
