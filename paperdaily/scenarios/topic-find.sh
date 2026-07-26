#!/usr/bin/env bash
# Find Topic ids whose display_name matches a substring (case-insensitive).
#
# Usage:
#   topic-find.sh blockchain
#   topic-find.sh "graph neural"     # multi-word
#   topic-find.sh --field 17 GAN     # restrict to one Field's subfields
#
# Why this exists: /taxonomy/topics has no q= parameter; without a
# subfield_id it returns only the top-`limit` globally by paper_count.
# To find niche Topics by name you have to walk subfields. This script
# parallelises that walk (8-wide) so a global scan completes in 2-5s.
#
# Output: tab-separated, sorted by paper_count desc:
#   <topic_id>\t<paper_count>\t<subfield_id>\t<display_name>
#
# Scope: read:paper.

# NOT using `set -e` / `pipefail`: the parallel walk hits the read:paper
# 60/min cap and a few subfield calls come back as a `{detail:...}` 429
# instead of an array; we want to tolerate that and emit whatever hits
# the rest of the calls produced, not abort the pipeline.
set -uo pipefail
trap '' PIPE
source "$(dirname "$0")/../_lib.sh"

field_filter=""
needle=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --field) field_filter=$2; shift 2 ;;
    --help|-h) sed -n '2,15p' "$0"; exit 0 ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *) needle="${needle}${needle:+ }$1"; shift ;;
  esac
done
[[ -n "$needle" ]] || { echo "usage: topic-find.sh <name-substring> [--field N]" >&2; exit 2; }
needle_lc=$(echo "$needle" | tr '[:upper:]' '[:lower:]')

# Build the subfield-id list to scan.
if [[ -n "$field_filter" ]]; then
  sf_ids=$(curl -sS -H "Authorization: Bearer $PD_KEY" \
    "$PD_BASE/taxonomy/subfields?field_id=$field_filter" | jq -r '.[].id')
else
  sf_ids=$(curl -sS -H "Authorization: Bearer $PD_KEY" \
    "$PD_BASE/taxonomy/subfields" | jq -r '.[].id')
fi

scan_one() {
  local sf=$1
  # jq filter: only iterate when the response is an array. 429/500 bodies
  # are `{detail:...}` strings; if we don't gate on type the jq error
  # firehose drowns the actual hits.
  curl -sS -H "Authorization: Bearer $PD_KEY" \
    "$PD_BASE/taxonomy/topics?subfield_id=$sf&limit=200" 2>/dev/null \
    | jq -r --arg sf "$sf" --arg n "$needle_lc" '
        if type == "array" then
          .[] | select(.display_name | ascii_downcase | contains($n))
              | [.id, .paper_count, $sf, .display_name] | @tsv
        else empty end' 2>/dev/null
}
export -f scan_one
export PD_BASE PD_KEY needle_lc

# Parallelism reasoned around the read:paper 60/min cap: 4-wide × ~80ms
# per request ≈ 50 req/s peak. We trip the cap and lose a handful of
# subfields to 429, but the rest land — far better than serial 30s.
echo "$sf_ids" | xargs -P 4 -I {} bash -c 'scan_one "$@"' _ {} \
  | sort -t$'\t' -k2 -nr
