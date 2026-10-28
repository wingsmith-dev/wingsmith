# Build context is `libs/`, not `libs/agent/`; see the note in gateway/Dockerfile. The agent requires
# libs/packages/tunnel through a `replace`, and a path outside the context does not exist to the
# builder.
FROM golang:1.27.2-alpine@sha256:f92b6ef800e499660581efdabdf25d9d817a9d124eaf900924f0504e7e27e12d AS build
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
# instead of repository permissions. That is also why a fine-grained token,
# whose package grants are repository-scoped, has no handle on it.
LABEL org.opencontainers.image.source=https://github.com/wingsmith-dev/wingsmith
WORKDIR /
COPY --from=build /out/agent /agent
USER 65532:65532
ENTRYPOINT ["/agent"]
