# The build context is `libs/`, not this directory: the image bakes the console bundle, and a
# context cannot reach above itself. Referenced as `plane/...` and `console/...` throughout.

# The console is built here rather than shipped as its own image so the plane is one thing to
# reach — see app/api/spa.py. `libs/console/Dockerfile` still builds the standalone nginx image
# for the day the console wants an origin of its own; nothing in the default deployment uses it.
FROM node:22.19.0-bookworm-slim AS console
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
# http://localhost:8000 only when the variable is *absent* — which is what a dev server wants and
# what an image serving its own console must not have. An empty value makes every request relative
# to wherever the bundle was loaded from, so the same image is correct behind a port-forward, a
# tunnel, or an ingress without being rebuilt for each.
ENV VITE_API_BASE_URL=""
ARG VITE_API_MODE
ARG VITE_API_PROJECT
ARG VITE_READ_ONLY
RUN pnpm run build

FROM python:3.12.8-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /bin/
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
RUN uv sync --locked --no-dev

FROM python:3.12.8-slim-bookworm

# psycopg is the pure-Python build and loads libpq at import time. A developer's machine has one
# already, which is why this is missing rather than wrong: the image had never been run. Installing
# the system library keeps the dependency pinned as it is, where switching to psycopg[binary] would
# vendor a second copy of libpq into the wheel and pin it to psycopg's release cadence.
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 65532 hangar && \
    useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin hangar

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --from=build /src/app ./app
COPY --from=build /src/migrations ./migrations
COPY --from=build /src/alembic.ini ./
COPY --from=console /src/dist ./app/api/static

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HANGAR_APP_ENV=production

COPY --chmod=755 plane/docker-entrypoint.sh /docker-entrypoint.sh

USER 65532:65532
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["api"]
