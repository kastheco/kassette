# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.10.8 AS uv

FROM python:3.12.13-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /bin/
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12.13-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install --yes --no-install-recommends libportaudio2 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system kassette \
    && useradd --system --gid kassette --home-dir /app kassette

WORKDIR /app
COPY --from=builder --chown=kassette:kassette /app /app

USER kassette
EXPOSE 7860

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ['PORT'] + '/healthz', timeout=2).read(256)"

CMD ["kassette", "serve", "--hosted"]
