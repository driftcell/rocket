# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.13.1 AS uv

FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS builder

COPY --from=uv /uv /uvx /bin/

# UV_LINK_MODE=copy: the venv is copied into the runtime stage, so it must not
# hardlink into the cache mount. UV_PYTHON_DOWNLOADS=0: always use the base
# image's interpreter instead of downloading a managed one.
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Dependencies first: this layer stays cached until pyproject.toml or uv.lock changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-dev --no-install-project

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

# Lets GHCR link the package back to this repository (also for local builds,
# where docker/metadata-action labels are absent).
LABEL org.opencontainers.image.source="https://github.com/driftcell/rocket"

RUN groupadd --system --gid 10001 rocket \
    && useradd --system --gid rocket --uid 10001 --create-home rocket \
    && mkdir /data \
    && chown rocket:rocket /data

COPY --from=builder /app/.venv /app/.venv

USER rocket
WORKDIR /data
ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["rocket"]
