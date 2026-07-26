#!/usr/bin/env bash
# scripts/cli/_lib.sh — shared helpers sourced by every scenario script.
#
# Loads PD_BASE + PD_KEY from ~/.paperdaily-cli/env (mode 600), and defines
# pd_get/pd_post/pd_delete that print the curl-equivalent banner before the
# response so the doc transcripts are self-describing.

set -euo pipefail

if [[ -z "${PD_KEY:-}" ]]; then
  [[ -r "$HOME/.paperdaily-cli/env" ]] || {
    echo "scripts/cli: missing ~/.paperdaily-cli/env" >&2
    echo "  create with: PD_BASE=https://www.paperdaily.org/api/v1  PD_KEY=pd_live_..." >&2
    exit 1
  }
  # shellcheck disable=SC1090
  source "$HOME/.paperdaily-cli/env"
fi
: "${PD_BASE:?PD_BASE not set}"
: "${PD_KEY:?PD_KEY not set}"

if [[ -t 1 ]]; then
  _PD_DIM=$'\e[2m'; _PD_BOLD=$'\e[1m'; _PD_RST=$'\e[0m'
else
  _PD_DIM=; _PD_BOLD=; _PD_RST=
fi

pd_banner() {
  local method=$1 path=$2
  printf '%s$ curl -H "Authorization: Bearer $PD_KEY" -X %s "$PD_BASE%s"%s\n' \
    "$_PD_BOLD" "$method" "$path" "$_PD_RST"
}

pd_get() {
  local path=$1 filter=${2:-.}
  pd_banner GET "$path"
  curl -sS -H "Authorization: Bearer $PD_KEY" "$PD_BASE$path" | jq "$filter"
}

pd_post() {
  local path=$1 body=$2 filter=${3:-.}
  pd_banner POST "$path"
  printf '%sbody:%s %s\n' "$_PD_DIM" "$_PD_RST" "$body"
  curl -sS -H "Authorization: Bearer $PD_KEY" -H 'Content-Type: application/json' \
    -X POST -d "$body" "$PD_BASE$path" | jq "$filter"
}

pd_delete() {
  local path=$1 filter=${2:-.}
  pd_banner DELETE "$path"
  curl -sS -X DELETE -H "Authorization: Bearer $PD_KEY" "$PD_BASE$path" | jq "$filter"
}
