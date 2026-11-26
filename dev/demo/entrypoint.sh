#!/bin/sh
set -eu

mode="${1:-fleet}"
[ $# -gt 0 ] && shift || true

: "${WINGSMITH_API:=http://localhost:8000}"
export WINGSMITH_API

wait_for_plane() {
  limit="${WINGSMITH_DEMO_WAIT:-300}"
  waited=0
  until python -c "import sys,urllib.request; urllib.request.urlopen(sys.argv[1] + '/health', timeout=3)" "${WINGSMITH_API%/v1}" 2>/dev/null; do
    if [ "$waited" -ge "$limit" ]; then
      echo "demo: plane at $WINGSMITH_API not healthy after ${limit}s" >&2
      exit 1
    fi
    waited=$((waited + 2))
    sleep 2
  done
}

case "$mode" in
  fleet)
    # The fleet and the reset job both write to the plane as admin: point WINGSMITH_API at the
    # writable plane instance, not the public read-only one.
    wait_for_plane
    cd /opt/dev
    exec python fleet.py --serve "$@"
    ;;
  reset)
    : "${WINGSMITH_DATABASE_URL:?reset needs WINGSMITH_DATABASE_URL}"
    wait_for_plane
    echo "demo: dropping and recreating schema public"
    python - <<'PY'
import os
import psycopg
from sqlalchemy.engine import make_url

url = make_url(os.environ["WINGSMITH_DATABASE_URL"]).set(drivername="postgresql")
with psycopg.connect(url.render_as_string(hide_password=False), autocommit=True) as conn:
    conn.execute("drop schema public cascade")
    conn.execute("create schema public")
PY
    echo "demo: migrating"
    (cd /app && alembic upgrade head)
    echo "demo: seeding"
    cd /opt/dev
    exec python seed.py --demo "$@"
    ;;
  *)
    exec "$mode" "$@"
    ;;
esac
