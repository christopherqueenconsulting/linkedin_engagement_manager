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
    # ffmpeg + ffprobe render the marketing tutorial videos (issue #505)
    ffmpeg \
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
# No VITE_API_TOKEN (issue #950). A LEM-issued shared secret baked in here ends up inlined in a
# public bundle, i.e. held by every visitor and unrotatable without a rebuild. The SPA authenticates
# on its httpOnly session cookie; the bearer is a non-browser credential and stays server-side.
# Browser-side PostHog (issue #646). Empty key = the SPA ships with analytics fully disabled and
# never fetches the posthog-js chunk.
ARG VITE_POSTHOG_KEY=""
ENV VITE_POSTHOG_KEY=${VITE_POSTHOG_KEY}
ARG VITE_POSTHOG_HOST=""
ENV VITE_POSTHOG_HOST=${VITE_POSTHOG_HOST}
# Session replay (issue #649). Both empty = replay ON at the 10% default sample; the error trigger
# is unconditional. VITE_POSTHOG_REPLAY=false is the kill switch (needs a rebuild, like the key).
ARG VITE_POSTHOG_REPLAY=""
ENV VITE_POSTHOG_REPLAY=${VITE_POSTHOG_REPLAY}
ARG VITE_POSTHOG_REPLAY_SAMPLE=""
ENV VITE_POSTHOG_REPLAY_SAMPLE=${VITE_POSTHOG_REPLAY_SAMPLE}
# Source-map upload for error tracking (issue #648). The INJECTED bundle is what must ship — the
# chunk ids posthog-cli writes into the JS are how a captured stack finds its map — so this runs
# here, inside the stage that produces dist/, and not as a separate CI build.
ARG POSTHOG_CLI_ENV_ID=""
ENV POSTHOG_CLI_ENV_ID=${POSTHOG_CLI_ENV_ID}
ARG POSTHOG_CLI_HOST=""
ENV POSTHOG_CLI_HOST=${POSTHOG_CLI_HOST}
COPY ./src/cqc_lem/ui/package.json ./src/cqc_lem/ui/package-lock.json ./
RUN npm ci
COPY ./src/cqc_lem/ui ./
RUN npm run build
# Best-effort by construction: no token (dev/local builds) or a failed upload must never fail a
# release build — it only costs readable stack traces. The .map files are deleted either way, so a
# build that skips the upload still never serves them.
#
# ca-certificates is installed inside the conditional, not in the stage setup, so a build with no
# token doesn't pay for it. It is REQUIRED: posthog-cli is a Rust binary and node:22-slim carries no
# system CA store, so its HTTP client panics before it ever authenticates —
#   panicked ... reqwest::Error { kind: Builder, source: General("No CA certificates were loaded
#   from the system") }
# The `|| echo` swallowed that into a one-line "upload failed", which read like a bad token and hid
# the real cause for an entire release. npm works regardless because it does its own TLS.
RUN --mount=type=secret,id=posthog_cli_token \
    if [ -s /run/secrets/posthog_cli_token ] && [ -n "$POSTHOG_CLI_ENV_ID" ]; then \
      POSTHOG_CLI_TOKEN="$(cat /run/secrets/posthog_cli_token)"; export POSTHOG_CLI_TOKEN; \
      ( apt-get update -qq && apt-get install -y -qq --no-install-recommends ca-certificates \
        && rm -rf /var/lib/apt/lists/* \
        && npm install -g @posthog/cli \
        && posthog-cli sourcemap inject --directory dist \
        && posthog-cli sourcemap upload --directory dist ) \
        || echo "posthog-cli source-map upload failed — shipping minified stacks"; \
    else \
      echo "posthog-cli source-map upload skipped — no POSTHOG_CLI_TOKEN/POSTHOG_CLI_ENV_ID"; \
    fi; \
    find dist -name '*.map' -delete

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
