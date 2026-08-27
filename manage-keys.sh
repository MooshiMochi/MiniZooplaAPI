#!/usr/bin/env bash
#
# manage-keys.sh — create / list / revoke / delete Mini Zoopla API keys via the live API.
# Works on Linux, macOS, and Windows (Git Bash / WSL).
#
# Reads config from .env in the script's directory (or current dir):
#   MINI_ZOOPLA_ADMIN_KEY   (required) — the admin key that mints others
#   MINI_ZOOPLA_HOST        (default 127.0.0.1)
#   MINI_ZOOPLA_PORT        (default 8000)
#
# Commands:
#   list                                   List all keys (active + revoked)
#   create <owner> [rate_limit]            Create a key (NOT bound to any branch)
#   revoke <key_id>                        Soft delete: deactivate the key (kept in DB)
#   delete <key_id>                        Hard delete: purge the key from the database
#   show <key_id>                          Show one key's details
#   help [command]                         Show usage (optionally for one command)
#
# Any command accepts -h / --help for its own usage.
#
# Examples:
#   ./manage-keys.sh create sheets_user 60
#   ./manage-keys.sh revoke a1b2c3d4e5f6
#   ./manage-keys.sh delete a1b2c3d4e5f6
#
set -euo pipefail

usage_all() {
  grep '^#' "$0" | sed 's/^# \{0,1\}//'
}

usage_cmd() {
  case "$1" in
    list)    echo "Usage: $0 list";;
    create)  echo "Usage: $0 create <owner> [rate_limit]";;
    revoke)  echo "Usage: $0 revoke <key_id>   (soft delete — key stays in DB, deactivated)";;
    delete)  echo "Usage: $0 delete <key_id>   (hard delete — purged from DB)";;
    show)    echo "Usage: $0 show <key_id>";;
    help)    echo "Usage: $0 help [command]";;
    *)       echo "Unknown command: $1"; usage_all; exit 1;;
  esac
}

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

cmd="${1:-help}"
shift || true

# Per-command --help flag
case "${1:-}" in
  -h|--help) usage_cmd "$cmd"; exit 0;;
esac

case "$cmd" in
  list)
    curl -fsS -H "X-Admin-Key: ${ADMIN_KEY}" "${BASE}/admin/keys" \
      | python3 -m json.tool 2>/dev/null || curl -fsS -H "X-Admin-Key: ${ADMIN_KEY}" "${BASE}/admin/keys"
    ;;
  create)
    [ $# -ge 1 ] || { usage_cmd create >&2; exit 1; }
    owner="$1"; limit="${2:-}"
    payload=$(printf '{"owner":%s' "$(python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "$owner")")
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
    [ $# -ge 1 ] || { usage_cmd revoke >&2; exit 1; }
    curl -fsS -X DELETE -H "X-Admin-Key: ${ADMIN_KEY}" "${BASE}/admin/keys/$1"
    echo ""
    ;;
  delete)
    [ $# -ge 1 ] || { usage_cmd delete >&2; exit 1; }
    curl -fsS -X DELETE -H "X-Admin-Key: ${ADMIN_KEY}" "${BASE}/admin/keys/$1?purge=true"
    echo ""
    ;;
  show)
    [ $# -ge 1 ] || { usage_cmd show >&2; exit 1; }
    curl -fsS -H "X-Admin-Key: ${ADMIN_KEY}" "${BASE}/admin/keys" \
      | python3 -c "import json,sys; d=json.load(sys.stdin); k=[x for x in d['keys'] if x['key_id']==sys.argv[1]]; print(json.dumps(k[0] if k else {'error':'not found'}, indent=2))" "$1"
    ;;
  ""|-h|--help|help)
    if [ -n "${1:-}" ] && [ "$1" != "-h" ] && [ "$1" != "--help" ]; then
      usage_cmd "$1"
    else
      usage_all
    fi
    exit 0
    ;;
  *)
    echo "Unknown command: $cmd" >&2
    usage_all >&2
    exit 1
    ;;
esac
