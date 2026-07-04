# titan-migrate — Alembic + migration scripts ONLY (Vol 9 §3.3). No FastAPI,
# no uvicorn: dependencies come from the dedicated `migrate` group and only
# settings + migrations are copied in. Runs `alembic upgrade head` and exits.
# Build context: apps/api
# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base
RUN pip install --no-cache-dir uv==0.11.7
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --only-group migrate

FROM python:3.12-slim AS runtime
RUN groupadd --system titan && useradd --system --gid titan --home-dir /app titan
WORKDIR /app
COPY --from=base --chown=titan:titan /app/.venv /app/.venv
COPY --chown=titan:titan alembic.ini ./
COPY --chown=titan:titan alembic ./alembic
COPY --chown=titan:titan src/titan/__init__.py ./src/titan/__init__.py
COPY --chown=titan:titan src/titan/settings.py ./src/titan/settings.py
COPY --chown=titan:titan src/titan/observability/__init__.py ./src/titan/observability/__init__.py
COPY --chown=titan:titan src/titan/observability/logging.py ./src/titan/observability/logging.py
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1
USER titan
CMD ["alembic", "upgrade", "head"]
