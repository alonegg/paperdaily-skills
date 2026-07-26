#!/usr/bin/env bash
# Scenario 1: "I care about field/subfield/topic X — give me the latest
# papers and related papers."
#
# Usage:
#   field-digest.sh <name-or-id> [--limit N] [--year-from Y] [--similar K] [--no-extraction]
#
# The first arg resolves to a Field, Subfield, or Topic depending on shape:
#   - 1-2 digit id (e.g. 17)                → Field
#   - 4-digit id (e.g. 1702)                → Subfield
#   - T-prefixed id (e.g. T10270)           → Topic
#   - "Field name" / "Subfield name" exact  → Field / Subfield by name
#   - other string                          → topic-find slow path (~3-5s)
#
# Examples:
#   field-digest.sh "Artificial Intelligence"
#   field-digest.sh 1702                        # subfield_id directly
#   field-digest.sh T10270 --year-from 2024     # blockchain topic, multi-year
#   field-digest.sh blockchain --year-from 2024 # topic name → topic-find
#   field-digest.sh "Finance" --limit 5 --year-from 2026
#   field-digest.sh 17 --limit 3 --similar 3    # field-level, with neighbour expansion
#
# Output is a markdown digest that lists:
#   - Resolved taxon (Field or Subfield) and what was matched
#   - Top N most-recent papers (filtered to ones with LLM extraction by default)
#   - For each, 3 nearest-neighbour papers via SPECTER2/BGE-M3 cosine
#   - A short LLM-synthesized "what's interesting here" note (one /ask call)
#
# Scopes used: read:paper, read:digest implicit via /ask, synth:ask (for the
# one summary call at the end; skip with --no-synth to save tokens).

set -euo pipefail
source "$(dirname "$0")/../_lib.sh"

LIMIT=10
YEAR_FROM=$(date +%Y)        # current year as default lower bound
SIMILAR_K=3
HAS_EXTRACTION=true
DO_SYNTH=1

target_raw=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit)         LIMIT=$2; shift 2 ;;
    --year-from)     YEAR_FROM=$2; shift 2 ;;
    --similar)       SIMILAR_K=$2; shift 2 ;;
    --no-extraction) HAS_EXTRACTION=false; shift ;;
    --no-synth)      DO_SYNTH=0; shift ;;
    --help|-h)       sed -n '2,20p' "$0"; exit 0 ;;
    -*)              echo "unknown flag: $1" >&2; exit 2 ;;
    *)               target_raw="${target_raw}${target_raw:+ }$1"; shift ;;
  esac
done
[[ -n "$target_raw" ]] || { echo "usage: field-digest.sh <field-or-subfield-name-or-id> [flags]" >&2; exit 2; }

# ── 1. resolve target to (level, id, display_name) ──────────────────
# Tiered resolution:
#   T-prefixed id   → topic
#   ≤2 numeric      → field
#   4 numeric       → subfield
#   string          → F exact → SF exact → SF substring → Topic name
#                     (Topic name path shells out to topic-find.sh,
#                     ~3-5s for a global subfield walk)
resolve_taxon() {
  local raw="$1"
  if [[ "$raw" =~ ^T[0-9]+$ ]]; then echo "topic|$raw"; return; fi
  if [[ "$raw" =~ ^[0-9]+$ ]]; then
    if [[ ${#raw} -le 2 ]]; then echo "field|$raw"; return; fi
    if [[ ${#raw} -eq 4 ]]; then echo "subfield|$raw"; return; fi
  fi
  local fields subfields hit
  fields=$(curl -sS -H "Authorization: Bearer $PD_KEY" "$PD_BASE/taxonomy/fields")
  hit=$(echo "$fields" | jq -r --arg q "$raw" \
    '[.[] | select(.display_name | ascii_downcase == ($q|ascii_downcase))][0] // empty | "field|" + .id + "|" + .display_name')
  if [[ -n "$hit" ]]; then echo "$hit"; return; fi
  subfields=$(curl -sS -H "Authorization: Bearer $PD_KEY" "$PD_BASE/taxonomy/subfields")
  hit=$(echo "$subfields" | jq -r --arg q "$raw" \
    '[.[] | select(.display_name | ascii_downcase == ($q|ascii_downcase))][0] // empty | "subfield|" + .id + "|" + .display_name')
  if [[ -n "$hit" ]]; then echo "$hit"; return; fi
  hit=$(echo "$subfields" | jq -r --arg q "$raw" \
    '[.[] | select(.display_name | ascii_downcase | contains($q|ascii_downcase))] | sort_by(-.paper_count) | .[0] // empty | "subfield|" + .id + "|" + .display_name')
  if [[ -n "$hit" ]]; then echo "$hit"; return; fi
  # Topic name path — parallel walk across all subfields. Slowest branch.
  echo "_(searching topics by name across all subfields, ~3-5s …)_" >&2
  local topic_hit
  topic_hit=$("$(dirname "$0")/topic-find.sh" "$raw" 2>/dev/null | head -1)
  if [[ -n "$topic_hit" ]]; then
    IFS=$'\t' read -r tid pcount sf tname <<<"$topic_hit"
    echo "topic|$tid|$tname"
    return
  fi
  echo ""  # not found
}

RESOLVED=$(resolve_taxon "$target_raw")
[[ -n "$RESOLVED" ]] || { echo "could not resolve '$target_raw' to a Field or Subfield" >&2; exit 3; }
IFS='|' read -r LEVEL TAX_ID TAX_NAME <<<"$RESOLVED"
TAX_NAME=${TAX_NAME:-$target_raw}

# Re-fetch display_name when input was a numeric/T id (resolve_taxon
# returned only the level|id pair for those cases).
if [[ -z "${TAX_NAME// }" || "$TAX_NAME" == "$target_raw" ]]; then
  case "$LEVEL" in
    field)
      TAX_NAME=$(curl -sS -H "Authorization: Bearer $PD_KEY" "$PD_BASE/taxonomy/fields" \
        | jq -r --arg id "$TAX_ID" '[.[] | select(.id==$id)][0].display_name // $id') ;;
    subfield)
      TAX_NAME=$(curl -sS -H "Authorization: Bearer $PD_KEY" "$PD_BASE/taxonomy/subfields" \
        | jq -r --arg id "$TAX_ID" '[.[] | select(.id==$id)][0].display_name // $id') ;;
    topic)
      # Global topic listing has limit=500 cap (Pydantic le=500). For a
      # Topic outside the top-500 by paper_count we just fall back to
      # showing the id. `if type=="array"` shields against 422/429 string
      # bodies that would otherwise crash the jq filter.
      TAX_NAME=$(curl -sS -H "Authorization: Bearer $PD_KEY" "$PD_BASE/taxonomy/topics?limit=500" \
        | jq -r --arg id "$TAX_ID" 'if type=="array" then [.[] | select(.id==$id)][0].display_name // $id else $id end') ;;
  esac
fi

echo "# Field digest — $TAX_NAME ($LEVEL=$TAX_ID)"
echo
echo "Filter: \`year_from=$YEAR_FROM\`, \`has_extraction=$HAS_EXTRACTION\`, \`limit=$LIMIT\`, \`similar_k=$SIMILAR_K\`"
echo

# ── 2. fetch recent papers in the taxon ─────────────────────────────
papers_q="/papers?${LEVEL}_id=${TAX_ID}&year_from=${YEAR_FROM}&limit=${LIMIT}"
[[ "$HAS_EXTRACTION" == "true" ]] && papers_q="${papers_q}&has_extraction=true"

papers_json=$(curl -sS -H "Authorization: Bearer $PD_KEY" "$PD_BASE$papers_q")
n_papers=$(echo "$papers_json" | jq '.items | length')

if [[ "$n_papers" == "0" ]]; then
  echo "_no papers matched — try --no-extraction or relax --year-from._"
  exit 0
fi

echo "## Latest $n_papers papers"
echo
echo "$papers_json" | jq -r '.items[] | "- **\(.title)** (`\(.id)`, \(.publication_date // "no date"))"'
echo

# ── 3. for each paper, fetch detail + similar neighbours ───────────
echo "## Per-paper detail (with $SIMILAR_K nearest neighbours)"
echo

paper_ids=$(echo "$papers_json" | jq -r '.items[].id')
for pid in $paper_ids; do
  detail=$(curl -sS -H "Authorization: Bearer $PD_KEY" "$PD_BASE/papers/$pid")
  title=$(echo "$detail" | jq -r '.title')
  doi=$(echo "$detail" | jq -r '.doi // ""')
  pub=$(echo "$detail" | jq -r '.publication_date // "?"')
  topic=$(echo "$detail" | jq -r '.primary_topic.topic_name // "?"')
  has_ext=$(echo "$detail" | jq -r '.has_extraction')

  echo "### $title"
  echo "- id: \`$pid\`  •  pub: $pub  •  topic: $topic"
  [[ -n "$doi" ]] && echo "- doi: $doi"
  echo "- has_extraction: $has_ext"

  similar=$(curl -sS -H "Authorization: Bearer $PD_KEY" "$PD_BASE/papers/$pid/similar?k=$SIMILAR_K")
  n_sim=$(echo "$similar" | jq '.items | length')
  if [[ "$n_sim" -gt 0 ]]; then
    echo "- similar:"
    echo "$similar" | jq -r '.items[] | "  - (dist=\(.distance | (.*1000|round)/1000)) **\(.paper.title)** — `\(.paper.id)`"'
  else
    echo "- similar: _none (paper has no embedding)_"
  fi
  echo
done

# ── 4. optional LLM synthesis ────────────────────────────────────────
if [[ "$DO_SYNTH" == "1" ]]; then
  echo "## LLM synthesis"
  echo
  # Force the LLM to actually look at *these* papers, not whatever its
  # keyword-semantic tool happens to return — `find_papers_by_keyword_semantic`
  # has no year filter and tends to surface 2018-2022 surveys instead of
  # the days-old papers we just listed.
  ids_csv=$(echo "$paper_ids" | paste -sd, -)
  question=$(cat <<EOF
Use the tool \`load_extractions(paper_ids=[$ids_csv])\` to fetch the
abstracts and extractions of these specific papers (do NOT call
find_papers_by_keyword_semantic — the papers are listed by id).

Then answer:
1. What 2-3 emerging research threads do these papers represent in $TAX_NAME?
2. Which 1-2 papers would you recommend reading first, and why?

If a paper has no cached extraction, work from its title alone and say so.
Cite each paper by its id (e.g. \`arxiv:2605.21488\`) when discussing it.
EOF
)
  body=$(jq -nc --arg q "$question" '{question:$q, max_iters:3}')
  resp=$(curl -sS -H "Authorization: Bearer $PD_KEY" -H 'Content-Type: application/json' \
    -X POST -d "$body" "$PD_BASE/ask")
  echo "$resp" | jq -r '
    "_tokens: in=\(.tokens_in) out=\(.tokens_out), \(.cited_paper_ids | length) cited_paper_ids; trace tools: \([.trace[]?.name] | join(", "))_\n",
    .answer
  '
fi
