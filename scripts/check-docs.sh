#!/usr/bin/env bash
# Fails on a dead reference in the tracked Markdown: a relative link or #anchor that resolves to
# nothing, or a backticked repository path (`libs/...`, `docs/...`, `scripts/...`, `dev/...`,
# `.github/...`) that does not exist.
#
#   check-docs.sh
#
# Links are checked offline by lychee, which also resolves heading anchors; external URLs are
# left alone, so the result does not depend on the network. lychee runs from PATH when it is
# installed and from its pinned image otherwise. A backticked path may be relative to the repo
# root or to the file, may end in :LINE, and is skipped when it holds a placeholder (<, *, {, $).
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
cd "$root"
lychee_image="lycheeverse/lychee:0.24.2@sha256:e2d19e57cf6ab037026f20b8e449a1f30d9d7f81eef4194763aab2eab20bd28d"

failed=0

mapfile -d '' docs < <(git ls-files -z '*.md')
lychee_args=(--offline --include-fragments --no-progress)
if command -v lychee >/dev/null; then
  lychee "${lychee_args[@]}" "${docs[@]}" || failed=1
else
  docker run --rm -v "$root:/repo:ro" -w /repo "$lychee_image" "${lychee_args[@]}" "${docs[@]}" || failed=1
fi

python3 - "${docs[@]}" <<'PY' || failed=1
import os
import re
import sys

path = re.compile(r"`((?:\.github|libs|docs|scripts|dev)/[^`\s]+)`")
missing = []
for doc in sys.argv[1:]:
    with open(doc, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            for match in path.finditer(line):
                ref = re.sub(r":\d+(-\d+)?$", "", match.group(1).rstrip("/.,:;)"))
                if any(char in ref for char in "<>*{}$"):
                    continue
                if not (os.path.exists(ref) or os.path.exists(os.path.join(os.path.dirname(doc), ref))):
                    missing.append(f"{doc}:{number}: `{ref}` does not exist")
for line in missing:
    print(line)
sys.exit(1 if missing else 0)
PY

if [ "$failed" -ne 0 ]; then
  echo "check-docs: dead references above" >&2
  exit 1
fi
echo "check-docs: ${#docs[@]} files, every relative link, anchor and backticked path resolves"
