#!/usr/bin/env bash
#
# manage-keys.sh — create / list / revoke Mini Zoopla API keys via the live API.
# Works on Linux, macOS, and Windows (Git Bash / WSL).
#
# Reads config from .env in the script's directory (or current dir):
#   MINI_ZOOPLA_ADMIN_KEY   (required) — the admin key that mints others
#   MINI_ZOOPLA_HOST        (default 127.0.0.1)
#   MINI_ZOOPLA_PORT        (default 8000)
#
# Usage:
#   ./manage-keys.sh list
#   ./manage-keys.sh create <owner> [allowed_branches_csv] [rate_limit]
#   ./manage-keys.sh revoke <key_id>
#   ./manage-keys.sh show   <key_id>
#
# Examples:
#   ./manage-keys.sh create sheets_user 56042,12345 60
#   ./manage-keys.sh revoke a1b2c3d4e5f6
#
set -euo pipefail

# Locate .env next to this script, then cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/.env}"
[ -f "$ENV_FILE" ] || ENV_FILE="./.env"

# Minimal .env loader (real env vars win). Uses python for safe parsing.
if [ -f "$ENV_FILE" ]; then
  # Convert MSYS path to native Windows path if needed (for native python3.exe).
  ENV_FILE_NATIVE="$ENV_FILE"
  if command -v cygpath >/dev/null 2>&1; then
    ENV_FILE_NATIVE="$(cygpath -w "$ENV_FILE")"
  fi
  eval "$(python3 -c '
import os, sys
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    k, v = k.strip(), v.strip().strip(chr(34)).strip(chr(39)).strip()
    if k and k not in os.environ:
        print("export " + k + "=" + repr(v))
' "$ENV_FILE_NATIVE")"
fi

ADMIN_KEY="${MINI_ZOOPLA_ADMIN_KEY:-}"
HOST="${MINI_ZOOPLA_HOST:-127.0.0.1}"
PORT="${MINI_ZOOPLA_PORT:-8000}"
BASE="http://${HOST}:${PORT}"

if [ -z "$ADMIN_KEY" ]; then
  echo "ERROR: MINI_ZOOPLA_ADMIN_KEY is not set." >&2
  echo "Set it in your .env file or export it before running this script." >&2
  exit 1
fi

cmd="${1:-}"
shift || true

case "$cmd" in
  list)
    curl -fsS -H "X-Admin-Key: ${ADMIN_KEY}" "${BASE}/admin/keys" \
      | python3 -m json.tool 2>/dev/null || curl -fsS -H "X-Admin-Key: ${ADMIN_KEY}" "${BASE}/admin/keys"
    ;;
  create)
    [ $# -ge 1 ] || { echo "Usage: $0 create <owner> [allowed_branches_csv] [rate_limit]" >&2; exit 1; }
    owner="$1"; branches="${2:-}"; limit="${3:-}"
    payload=$(printf '{"owner":%s' "$(python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "$owner")")
    if [ -n "$branches" ]; then
      IFS=',' read -ra arr <<< "$branches"
      bjson=$(printf '%s\n' "${arr[@]}" | python3 -c "import json,sys;print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))")
      payload="${payload},\"allowed_branches\":${bjson}"
    fi
    [ -n "$limit" ] && payload="${payload},\"rate_limit\":${limit}"
    payload="${payload}}"
    echo "Creating key: ${payload}"
    resp=$(curl -fsS -X POST -H "X-Admin-Key: ${ADMIN_KEY}" -H "Content-Type: application/json" \
      -d "$payload" "${BASE}/admin/keys")
    echo "$resp"
    echo ""
    echo "NOTE: the 'key' value above is shown only once. Store it securely." >&2
    ;;
  revoke)
    [ $# -ge 1 ] || { echo "Usage: $0 revoke <key_id>" >&2; exit 1; }
    curl -fsS -X DELETE -H "X-Admin-Key: ${ADMIN_KEY}" "${BASE}/admin/keys/$1"
    echo ""
    ;;
  show)
    [ $# -ge 1 ] || { echo "Usage: $0 show <key_id>" >&2; exit 1; }
    curl -fsS -H "X-Admin-Key: ${ADMIN_KEY}" "${BASE}/admin/keys" \
      | python3 -c "import json,sys; d=json.load(sys.stdin); k=[x for x in d['keys'] if x['key_id']==sys.argv[1]]; print(json.dumps(k[0] if k else {'error':'not found'}, indent=2))" "$1"
    ;;
  ""|-h|--help|help)
    grep '^#' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 0
    ;;
  *)
    echo "Unknown command: $cmd" >&2
    echo "Run '$0 help' for usage." >&2
    exit 1
    ;;
esac
