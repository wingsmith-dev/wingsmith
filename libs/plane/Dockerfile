# The build context is `libs/`, not this directory: the image can bake the console bundle, and a
# context cannot reach above itself. Referenced as `plane/...` and `console/...` throughout.

# CONSOLE=bundled (the default) builds the console and serves it from the API process, so the
# plane is one thing to reach (see app/api/spa.py). CONSOLE=none skips the Node build and ships an
# API-only image, which is what a deployment that serves the console elsewhere, or a dev loop
# that runs the console dev server, wants.
ARG CONSOLE=bundled

FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0 AS console-none
RUN mkdir -p /src/dist

FROM node:22.19.0-bookworm-slim@sha256:4a4884e8a44826194dff92ba316264f392056cbe243dcc9fd3551e71cea02b90 AS console-bundled
WORKDIR /src

ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH
RUN corepack enable && corepack prepare pnpm@10.15.1 --activate

COPY console/package.json console/pnpm-lock.yaml ./
RUN --mount=type=cache,id=pnpm,target=/pnpm/store \
    pnpm install --frozen-lockfile

COPY console/ ./

# Empty, not unset. Vite inlines these at build time, and `http.ts` falls back to
# http://localhost:8000 only when the variable is *absent*, which is what a dev server wants and
# what an image serving its own console must not have. An empty value makes every request relative
# to wherever the bundle was loaded from, so the same image is correct behind a port-forward, a
# tunnel, or an ingress without being rebuilt for each.
ENV VITE_API_BASE_URL=""
ARG VITE_API_MODE
ARG VITE_READ_ONLY
RUN pnpm run build

FROM console-${CONSOLE} AS console

FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0 AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f73a8c4ef5d69f4e6fbbdb8af751d6fa40ec34b39a1ab469acd6e289b7 /uv /uvx /bin/
WORKDIR /src

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY plane/pyproject.toml plane/uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

COPY plane/app ./app
COPY plane/migrations ./migrations
COPY plane/alembic.ini ./
COPY plane/README.md ./
# The device-class table is shared with the operator and lives in the contract, not in this
# package. `app/core/devices.py` reads it at import, so an image without it cannot answer a
# metrics request: it fails resolving the path before it fails finding the file.
COPY packages/contract/devices.json ./contract/devices.json
RUN uv sync --locked --no-dev

FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0

# psycopg is the pure-Python build and loads libpq at import time. A developer's machine has one
# already, which is why this is missing, not wrong: the image had never been run. Installing
# the system library keeps the dependency pinned as it is, where switching to psycopg[binary] would
# vendor a second copy of libpq into the wheel and pin it to psycopg's release cadence.
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 65532 wingsmith && \
    useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin wingsmith

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --from=build /src/app ./app
COPY --from=build /src/migrations ./migrations
COPY --from=build /src/alembic.ini ./
COPY --from=build /src/contract ./contract
COPY --from=console /src/dist ./app/api/static

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WINGSMITH_APP_ENV=production

COPY --chmod=755 plane/docker-entrypoint.sh /docker-entrypoint.sh

USER 65532:65532
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["api"]
