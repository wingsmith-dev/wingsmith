FROM python:3.12.8-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /bin/
WORKDIR /src

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY README.md ./
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

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HANGAR_APP_ENV=production

COPY --chmod=755 docker-entrypoint.sh /docker-entrypoint.sh

USER 65532:65532
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["api"]
