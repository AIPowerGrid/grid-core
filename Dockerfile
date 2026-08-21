# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements-grid.txt requirements-grid.lock ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --require-hashes --only-binary=:all: \
        -r requirements-grid.lock \
    && /opt/venv/bin/pip check

FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS runtime

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 aipg

COPY --from=build /opt/venv /opt/venv

WORKDIR /app
COPY --chown=aipg:aipg . .

USER aipg

EXPOSE 7002

CMD ["uvicorn", "grid_api.main:app", "--host", "0.0.0.0", "--port", "7002", "--proxy-headers", "--forwarded-allow-ips=*"]
