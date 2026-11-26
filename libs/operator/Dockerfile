FROM golang:1.27.4-alpine@sha256:70bd7ddea8b6cbe5d6943cde279d6b67feb10cc924fb9100d1d8757c1575b280 AS build
WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download

COPY . .
ARG VERSION=dev
RUN CGO_ENABLED=0 go build -trimpath \
      -ldflags "-s -w -X main.version=${VERSION}" \
      -o /out/operator ./cmd/operator

FROM gcr.io/distroless/static:nonroot@sha256:18f4cc26c9ffa7f20a359298be7deadac5c607c8e03a02b8119f1a5fc2f82c99
# Links the GHCR package to this repository. Without it a pushed package is owned
# by the org and attached to nothing, so its access comes from a per-package list
# instead of repository permissions. That is also why a fine-grained token,
# whose package grants are repository-scoped, has no handle on it.
LABEL org.opencontainers.image.source=https://github.com/wingsmith-dev/wingsmith
WORKDIR /
COPY --from=build /out/operator /operator
USER 65532:65532
ENTRYPOINT ["/operator"]
