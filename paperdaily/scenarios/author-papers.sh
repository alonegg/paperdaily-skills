#!/usr/bin/env bash
# Scenario 2: "Recent papers by author NAME."
#
# Usage:
#   author-papers.sh "Yoshua Bengio" [--limit N] [--all-matches] [--synth]
#
# Strategy (refactored 2026-05-22 after Bug E/F retraction):
#   1. /authors?q=NAME              → returns rows with real `id` (A-IDs).
#                                     Sorted by paper_count desc.
#   2. /authors/{top-id}/papers     → recent papers for the most-prolific
#                                     match. Use --all-matches to also show
#                                     the runners-up (different people who
#                                     share the name).
#   3. (optional) POST /ask         → only if --synth: LLM summary built
#                                     from the paper ids we just collected.
#
# Why /ask is now opt-in: the REST path returns the same papers the LLM
# tool would call (same Cypher), so /ask only adds value if you want a
# narrative summary. It costs ~3-10k tokens.
#
# OpenAlex data-quality caveat: an A-id's paper_count + linked papers
# come from OpenAlex's per-author attribution, which is imperfect — the
# "famous Yoshua Bengio" record (A5086198262, paper_count=55) includes
# a HVAC-dataset paper and Li-ion cathode work alongside his consciousness
# papers, because OpenAlex hasn't disambiguated him fully. The skill
# surfaces what's there; don't editorialize the discrepancy unless the
# user asks.

set -euo pipefail
source "$(dirname "$0")/../_lib.sh"

LIMIT=8
ALL_MATCHES=0
DO_SYNTH=0
name=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit)        LIMIT=$2; shift 2 ;;
    --all-matches)  ALL_MATCHES=1; shift ;;
    --synth)        DO_SYNTH=1; shift ;;
    --help|-h)      sed -n '2,25p' "$0"; exit 0 ;;
    -*)             echo "unknown flag: $1" >&2; exit 2 ;;
    *)              name="${name}${name:+ }$1"; shift ;;
  esac
done
[[ -n "$name" ]] || { echo "usage: author-papers.sh \"Full Name\" [--limit N] [--all-matches] [--synth]" >&2; exit 2; }

qenc=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote_plus(sys.argv[1]))" "$name")

echo "# Author papers — $name"
echo

# ── 1. name lookup ──────────────────────────────────────────────────
authors_json=$(curl -sS -H "Authorization: Bearer $PD_KEY" "$PD_BASE/authors?q=$qenc&limit=5")
n=$(echo "$authors_json" | jq 'length')
if [[ "$n" == "0" ]]; then
  echo "_no name matches. Try a different spelling, or include an initial._"
  exit 0
fi

echo "## Name matches ($n total, ranked by paper_count)"
echo
echo "$authors_json" | jq -r '
  .[] | "- **\(.name)** — id=`\(.id)`, papers=\(.paper_count // 0)" +
        (if .orcid then ", orcid=\(.orcid)" else "" end) +
        (if .affiliations | length > 0 then ", aff=\(.affiliations | join(" / "))" else "" end)
'
echo

# ── 2. recent papers for the top match (and optionally the others) ──
if [[ "$ALL_MATCHES" == "1" ]]; then
  ids=$(echo "$authors_json" | jq -r '.[].id')
else
  ids=$(echo "$authors_json" | jq -r '.[0].id')
fi

all_paper_ids=()
for aid in $ids; do
  ainfo=$(echo "$authors_json" | jq -r --arg id "$aid" \
    '.[] | select(.id==$id) | "\(.name) — id=\(.id), papers=\(.paper_count // 0)"')
  echo "## Recent papers — $ainfo"
  echo
  papers_json=$(curl -sS -w "\n%{http_code}" -H "Authorization: Bearer $PD_KEY" \
    "$PD_BASE/authors/$aid/papers?limit=$LIMIT")
  code=$(echo "$papers_json" | tail -1)
  body=$(echo "$papers_json" | sed '$d')
  if [[ "$code" == "404" ]]; then
    echo "_no non-stub papers indexed under this author id._"
    echo
    continue
  fi
  echo "$body" | jq -r '.items[] | "- **\(.title)** — `\(.id)` (\(.publication_date // "no date"))" +
        (if .doi then ", doi=\(.doi)" else "" end) +
        (if .venue_name then ", venue=\(.venue_name)" else "" end)'
  echo
  # Collect ids for optional synth.
  while IFS= read -r pid; do all_paper_ids+=("$pid"); done < <(echo "$body" | jq -r '.items[].id')
done

# ── 3. optional LLM synthesis over collected paper ids ──────────────
if [[ "$DO_SYNTH" == "1" && ${#all_paper_ids[@]} -gt 0 ]]; then
  echo "## LLM synthesis"
  echo
  ids_csv=$(printf '%s,' "${all_paper_ids[@]}" | sed 's/,$//')
  question=$(cat <<EOF
Use the tool \`load_extractions(paper_ids=[$ids_csv])\` to pull the
extractions for these papers (all authored by people named "$name"
according to OpenAlex). Then summarize:
1. What broad themes do these papers span?
2. Which 1-2 papers look most impactful or novel, and why?
Cite each paper by id (e.g. \`W4416083181\`).
EOF
)
  body=$(jq -nc --arg q "$question" '{question:$q, max_iters:3}')
  resp=$(curl -sS -H "Authorization: Bearer $PD_KEY" -H 'Content-Type: application/json' \
    -X POST -d "$body" "$PD_BASE/ask")
  echo "$resp" | jq -r '
    "_tokens: in=\(.tokens_in) out=\(.tokens_out); trace tools: \([.trace[]?.name] | join(", "))_\n",
    .answer
  '
fi
