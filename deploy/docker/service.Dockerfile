# One Dockerfile for the gateway, api and worker images. Build from the repo root:
#   docker build -f deploy/docker/service.Dockerfile --build-arg SERVICE=gateway .
# The mock upstream uses SERVICE=mock-upstream and APP_MODULE=mock_upstream.app:create_app_from_env.
ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY services ./services
COPY tools ./tools
ARG SERVICE
RUN uv sync --locked --no-dev --no-editable --package "jarvis-${SERVICE}"

FROM ${PYTHON_IMAGE} AS runtime
ARG SERVICE
ARG APP_MODULE="jarvis_${SERVICE}.app:create_app_from_env"
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    APP_MODULE="${APP_MODULE}"
# Debian security fix (CVE-2026-84782) newer than the pinned base image; drop once the base has it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade libssl3 openssl \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 10001 jarvis \
    && useradd --system --uid 10001 --gid jarvis --no-create-home --shell /usr/sbin/nologin jarvis \
    && install -d -o 10001 -g 10001 -m 0700 /var/lib/jarvis/gateway
COPY --from=builder --chown=jarvis:jarvis /app/.venv /app/.venv
USER 10001:10001
WORKDIR /app
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn --factory \"$APP_MODULE\" --host 0.0.0.0 --port \"${PORT:-8080}\""]
