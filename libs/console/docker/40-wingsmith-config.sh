#!/bin/sh
# Writes the console's runtime settings from the container environment, so one image can point at
# any plane. Runs from the nginx image's /docker-entrypoint.d before nginx starts.
set -eu

html=/usr/share/nginx/html
base="${WINGSMITH_API_BASE_URL:-}"
read_only="${WINGSMITH_READ_ONLY:-false}"

case "$read_only" in
  true|false) ;;
  *) echo "WINGSMITH_READ_ONLY must be true or false, got '$read_only'" >&2; exit 1 ;;
esac

# A base URL is an origin plus an optional path. Refusing quotes, backslashes, whitespace,
# semicolons, '#' and '&' keeps the value from breaking out of the JS string, the CSP header or
# the sed substitution below.
case "$base" in
  *[\"\\\'\;\ \#\&]*) echo "WINGSMITH_API_BASE_URL contains characters a URL cannot: '$base'" >&2; exit 1 ;;
esac
case "$base" in
  ""|http://*|https://*) ;;
  *) echo "WINGSMITH_API_BASE_URL must be empty (same origin) or an http(s) URL, got '$base'" >&2; exit 1 ;;
esac

printf 'window.__WINGSMITH_CONFIG__ = {"apiBaseUrl": "%s", "readOnly": %s}\n' "$base" "$read_only" \
  > "$html/config.js"

# The API origin the CSP has to allow. Empty when the plane shares the console's origin.
origin=""
if [ -n "$base" ]; then
  origin=$(printf '%s' "$base" | sed -E 's#^(https?://[^/]+).*#\1#')
fi
for name in default security-headers; do
  sed "s#\${WINGSMITH_CONNECT_SRC}#${origin}#" "/etc/nginx/wingsmith/$name.conf.template" \
    > "/etc/nginx/wingsmith/$name.conf"
done
ln -sf /etc/nginx/wingsmith/default.conf /etc/nginx/conf.d/default.conf
