# One Dockerfile for the gateway, api and worker images. Build from the repo root:
#   docker build -f deploy/docker/service.Dockerfile --build-arg SERVICE=gateway .
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
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    APP_MODULE="jarvis_${SERVICE}.app:create_app_from_env"
RUN groupadd --system --gid 10001 jarvis \
    && useradd --system --uid 10001 --gid jarvis --no-create-home --shell /usr/sbin/nologin jarvis
COPY --from=builder --chown=jarvis:jarvis /app/.venv /app/.venv
USER 10001:10001
WORKDIR /app
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn --factory \"$APP_MODULE\" --host 0.0.0.0 --port \"${PORT:-8080}\""]
