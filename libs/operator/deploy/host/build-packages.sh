#!/usr/bin/env bash
# Builds the wingsmith-host binary and its .deb and .rpm for one architecture, into
# libs/operator/dist/. Run on a Linux machine of that architecture with Docker and Go installed:
#   VERSION=0.1.0 deploy/host/build-packages.sh
# NVML is loaded through cgo, so the binary is compiled natively inside an AlmaLinux 8 container:
# linking against glibc 2.28 keeps it running on RHEL/Rocky 8+, Debian 10+ and Ubuntu 20.04+.
set -euo pipefail

cd "$(dirname "$0")/../.."
: "${VERSION:?set VERSION, e.g. 0.1.0}"
NFPM_VERSION="${NFPM_VERSION:-v2.45.0}"
BUILD_IMAGE="${BUILD_IMAGE:-almalinux:8}"
case "$(uname -m)" in
x86_64) ARCH=amd64 ;;
aarch64 | arm64) ARCH=arm64 ;;
*) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac
export ARCH VERSION

goroot="$(go env GOROOT)"
mkdir -p dist
rm -f dist/wingsmith-host
docker run --rm \
	-v "$goroot:/usr/local/go:ro" \
	-v "$(go env GOMODCACHE):/gomod" \
	-v "$(cd ../.. && pwd):/src" -w /src/libs/operator \
	-e GOMODCACHE=/gomod -e GOCACHE=/tmp/gocache -e GOFLAGS=-mod=readonly -e CGO_ENABLED=1 \
	-e GOTOOLCHAIN=local -e VERSION \
	--user 0 "$BUILD_IMAGE" sh -ec '
		dnf -q -y install gcc >/dev/null
		/usr/local/go/bin/go build -trimpath -ldflags "-s -w" -o dist/wingsmith-host ./cmd/wingsmith-host
		chown '"$(id -u):$(id -g)"' dist/wingsmith-host'

nfpm=(go run "github.com/goreleaser/nfpm/v2/cmd/nfpm@$NFPM_VERSION")
for p in deb rpm; do
	"${nfpm[@]}" package -f deploy/host/nfpm.yaml -p "$p" -t dist/
done

# Print what went in, so a CI log shows the package contents.
for f in dist/*.deb; do dpkg-deb -c "$f"; done
for f in dist/*.rpm; do
	if command -v rpm >/dev/null; then rpm -qlpv "$f"; fi
done
