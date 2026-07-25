FROM python:3.13-slim AS base


ENV PYTHONFAULTHANDLER=1 \
    PYTHONHASHSEED=random \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install dependencies (When using python-slim)
RUN apt-get update -y && apt-get install --no-install-suggests --no-install-recommends --yes \
    # Curl needed for AWS health checks
    curl \
    netcat-traditional \
    # cleaning up unused files
    && apt-get purge -y --auto-remove -o APT::AutoRemove::RecommendsImportant=false \
    && rm -rf /var/lib/apt/lists/*

# Upgrade Pip
RUN python3 -m pip install --upgrade pip

WORKDIR /app

# ---------------------------------------------------------------------------
# Stage: build the React SPA so the dist/ folder exists inside the image.
# The dist/ directory is .gitignored, so it must be built here.
# ---------------------------------------------------------------------------
FROM node:22-slim AS ui-builder

WORKDIR /ui
# Build-time API bearer token baked into the SPA bundle (Vite reads VITE_*).
# Empty by default so dev images stay tokenless; CI passes the prod token.
ARG VITE_API_TOKEN=""
ENV VITE_API_TOKEN=${VITE_API_TOKEN}
COPY ./src/cqc_lem/ui/package.json ./src/cqc_lem/ui/package-lock.json ./
RUN npm ci
COPY ./src/cqc_lem/ui ./
RUN npm run build

FROM base AS builder

ARG INSTALL_DEV_DEPS=false
ARG POETRY_VERSION=2.0

ENV POETRY_HOME=/opt/poetry \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_VIRTUALENVS_CREATE=1 \
    POETRY_CACHE_DIR=/opt/.cache \
    PIP_DEFAULT_TIMEOUT=100 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install "poetry==$POETRY_VERSION" poetry-plugin-export

# Install deps first (cached layer — only busts when pyproject.toml/poetry.lock change)
COPY ./pyproject.toml ./poetry.lock ./
RUN if [ "$INSTALL_DEV_DEPS" = "true" ]; then \
    poetry install --with dev --no-root && rm -rf $POETRY_CACHE_DIR; \
    else \
    poetry install --only main --no-root && rm -rf $POETRY_CACHE_DIR; \
    fi

# Copy source and install the project package (busts when src changes)
COPY ./README.md ./README.md
COPY ./src ./src
COPY --from=ui-builder /ui/dist ./src/cqc_lem/ui/dist
RUN if [ "$INSTALL_DEV_DEPS" = "true" ]; then \
    poetry install --with dev && rm -rf $POETRY_CACHE_DIR; \
    else \
    poetry install --only main && rm -rf $POETRY_CACHE_DIR; \
    fi

# Clean runtime image — no Poetry, no pip build tools, just the venv and source
FROM base AS final

ENV VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# Create user BEFORE any COPY so we can use --chown at copy time instead of
# a separate RUN chown -R /app (which would duplicate every file in a new layer).
RUN useradd -m -s /bin/bash celeryworker && \
    mkdir -p /home/celeryworker/.aws /app/logs && \
    chown celeryworker:celeryworker /home/celeryworker/.aws /app/logs && \
    chmod 755 /app/logs

COPY --chown=celeryworker:celeryworker --from=builder /app/.venv /app/.venv
COPY --chown=celeryworker:celeryworker --from=builder /app/src /app/src

# Copy the built SPA straight from the ui-builder stage so it is guaranteed to
# land in the final image (the builder->final /app/src copy was dropping it,
# leaving FastAPI to 404 "/"). Assert it is present so this can't silently break.
COPY --chown=celeryworker:celeryworker --from=ui-builder /ui/dist /app/src/cqc_lem/ui/dist
RUN test -f /app/src/cqc_lem/ui/dist/index.html \
    || (echo "FATAL: SPA dist missing from image (ui build did not land)" && exit 1)

# Start scripts live at / so they are not inside the chowned /app tree
COPY ./compose/local/entrypoint /entrypoint
COPY ./compose/local/wait-for-it /wait-for-it
COPY ./compose/local/fastapi/start /start-fastapi
COPY ./compose/local/fastapi/start-cloud /start-fastapi-cloud
# Shared privilege-dropping exec-er — keeps celery as the signal-receiving process (issue #549)
COPY ./compose/local/celery/run-as-celery /run-as-celery
COPY ./compose/local/celery/worker/start /start-celeryworker
COPY ./compose/local/celery/worker/start-solo-pool /start-celeryworker-solo
COPY ./compose/local/celery/worker/start-selenium /start-celeryworker-selenium
COPY ./compose/local/celery/beat/start /start-celerybeat
COPY ./compose/local/celery/flower/start /start-flower
COPY ./compose/local/celery/flower/start-no-wait /start-flower-no-wait

RUN chmod +x /entrypoint /wait-for-it /start-fastapi /run-as-celery \
    /start-fastapi-cloud /start-celeryworker /start-celeryworker-solo \
    /start-celeryworker-selenium /start-celerybeat /start-flower /start-flower-no-wait

ENTRYPOINT ["/entrypoint"]
