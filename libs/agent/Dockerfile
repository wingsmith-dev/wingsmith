# Build context is `libs/`, not `libs/agent/` — see the note in gateway/Dockerfile. The agent requires
# libs/packages/tunnel through a `replace`, and a path outside the context does not exist to the
# builder.
FROM golang:1.25.14-alpine@sha256:1ae0735f00daffa3aaf1363a5184c0d2dc55c78e3db4ec70241cdac97bf84b59 AS build
WORKDIR /src

COPY packages/tunnel/ ./packages/tunnel/
COPY agent/go.mod agent/go.sum ./agent/
WORKDIR /src/agent
RUN go mod download

COPY agent/ ./
ARG VERSION=dev
RUN CGO_ENABLED=0 go build -trimpath \
      -ldflags "-s -w -X main.version=${VERSION}" \
      -o /out/agent ./cmd/agent

FROM gcr.io/distroless/static:nonroot@sha256:18f4cc26c9ffa7f20a359298be7deadac5c607c8e03a02b8119f1a5fc2f82c99
# Links the GHCR package to this repository. Without it a pushed package is owned
# by the org and attached to nothing, so its access comes from a per-package list
# rather than from repository permissions — which is also why a fine-grained token,
# whose package grants are repository-scoped, has no handle on it.
LABEL org.opencontainers.image.source=https://github.com/hangar-dev/hangar
WORKDIR /
COPY --from=build /out/agent /agent
USER 65532:65532
ENTRYPOINT ["/agent"]
