# The demo world as a container: the fake fleet (dev/fleet.py --serve) and the reset job
# (dev/seed.py --demo). Build context is the repository root:
#
#   docker build -f dev/demo/Dockerfile -t wingsmith-demo .
#
# It carries its own gateway, agent and wingstransit binaries because fleet.py --serve spawns one
# gateway and an agent plus stand-in engine per cluster. See dev/demo/README.md.

FROM golang:1.26.10-alpine@sha256:a2012b4a1d1f9f7e9f44e0d39b805a901e856b1dd80b4e6ee4338db547050308 AS go
WORKDIR /src
COPY libs/packages/tunnel/ ./libs/packages/tunnel/
COPY libs/gateway/ ./libs/gateway/
COPY libs/agent/ ./libs/agent/
COPY libs/transit/ ./libs/transit/
ENV CGO_ENABLED=0
RUN cd /src/libs/gateway && go build -trimpath -ldflags "-s -w" -o /out/gateway ./cmd/gateway
RUN cd /src/libs/agent && go build -trimpath -ldflags "-s -w" -o /out/agent ./cmd/agent
RUN cd /src/libs/transit && go build -trimpath -ldflags "-s -w" -o /out/wingstransit ./cmd/wingstransit

FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0 AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f73a8c4ef5d69f4e6fbbdb8af751d6fa40ec34b39a1ab469acd6e289b7 /uv /uvx /bin/
WORKDIR /src
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
# The plane's own locked environment: seed.py and fleet.py run in it (justfile: `cd libs/plane && uv run`),
# and the reset needs its alembic, psycopg and migrations.
COPY libs/plane/pyproject.toml libs/plane/uv.lock ./
RUN uv sync --locked --no-install-project --no-dev
COPY libs/plane/app ./app
COPY libs/plane/migrations ./migrations
COPY libs/plane/alembic.ini libs/plane/README.md ./
COPY libs/packages/contract/devices.json ./contract/devices.json
RUN uv sync --locked --no-dev

FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 && \
    apt-get clean && \
    find /var/lib/apt/lists -type f -delete
RUN groupadd --gid 65532 wingsmith && \
    useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin wingsmith

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --from=build /src/app ./app
COPY --from=build /src/migrations ./migrations
COPY --from=build /src/alembic.ini ./
COPY --from=build /src/contract ./contract
COPY --from=go /out/ /opt/bin/
COPY dev/fleet.py dev/seed.py dev/inference.py /opt/dev/
COPY --chmod=755 dev/demo/entrypoint.sh /entrypoint.sh

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WINGSMITH_APP_ENV=production \
    WINGSMITH_DEMO_BIN=/opt/bin \
    WINGSMITH_DEMO_LOGS=stdout \
    WINGSMITH_DEMO_GATEWAY=0.0.0.0:8080 \
    WINGSMITH_DEMO_TUNNEL=0.0.0.0:8443 \
    WINGSMITH_DEMO_TUNNEL_DIAL=127.0.0.1:8443 \
    WINGSMITH_DEMO_GATEWAY_METRICS=0.0.0.0:9090

EXPOSE 8080 8443 9090
USER 65532:65532
ENTRYPOINT ["/entrypoint.sh"]
CMD ["fleet"]
