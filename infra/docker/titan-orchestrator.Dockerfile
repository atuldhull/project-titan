# titan-orchestrator — at MVP the orchestrator module runs inside titan-core
# (ADR-BCK-01 modular monolith); this image exists per Vol 9 §3.3 so the
# scale-out extraction path is build-ready. It gains its own entrypoint when
# the orchestration engine lands (Vol 3 §8). Hardening: python:3.12-slim base,
# uv deps, non-root, read-only root FS, multi-arch.
# Build context: apps/api
# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base
RUN pip install --no-cache-dir uv==0.11.7
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
RUN uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime
RUN groupadd --system titan && useradd --system --gid titan --home-dir /app titan
WORKDIR /app
COPY --from=base --chown=titan:titan /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER titan
EXPOSE 8000
CMD ["uvicorn", "titan.main:app", "--host", "0.0.0.0", "--port", "8000"]
