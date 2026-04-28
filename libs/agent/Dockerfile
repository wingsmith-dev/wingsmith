# Build context is `libs/`, not `libs/agent/` — see the note in gateway/Dockerfile. The agent requires
# libs/packages/tunnel through a `replace`, and a path outside the context does not exist to the
# builder.
FROM golang:1.25-alpine AS build
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

FROM gcr.io/distroless/static:nonroot
WORKDIR /
COPY --from=build /out/agent /agent
USER 65532:65532
ENTRYPOINT ["/agent"]
