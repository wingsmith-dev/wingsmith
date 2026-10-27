# Build context is `libs/`, not `libs/gateway/`.
#
# The gateway requires libs/packages/tunnel through a `replace` pointing at ../packages/tunnel, and a
# path outside the context does not exist as far as the builder is concerned — the narrower context
# fails at `go mod download` with a missing replacement rather than anywhere informative. So the
# context is the parent and both module trees are copied in.
FROM golang:1.25.14-alpine@sha256:1ae0735f00daffa3aaf1363a5184c0d2dc55c78e3db4ec70241cdac97bf84b59 AS build
WORKDIR /src

# The wire first and on its own layer: it changes far less often than the gateway does, so a gateway
# edit reuses the download below instead of re-resolving both modules.
COPY packages/tunnel/ ./packages/tunnel/
COPY gateway/go.mod gateway/go.sum ./gateway/
WORKDIR /src/gateway
RUN go mod download

COPY gateway/ ./
ARG VERSION=dev
RUN CGO_ENABLED=0 go build -trimpath \
      -ldflags "-s -w -X main.version=${VERSION}" \
      -o /out/gateway ./cmd/gateway

FROM gcr.io/distroless/static:nonroot@sha256:18f4cc26c9ffa7f20a359298be7deadac5c607c8e03a02b8119f1a5fc2f82c99
WORKDIR /
COPY --from=build /out/gateway /gateway
USER 65532:65532
ENTRYPOINT ["/gateway"]
