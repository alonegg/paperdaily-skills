#!/usr/bin/env bash
# pd_worklist.sh — Stage 1 of the paperdaily deep-research skill: resolve a
# target (taxonomy node / free-text query / single seed paper), pull a paper
# worklist (+ similar-paper expansion), and optionally synthesize a short LLM
# overview grounded in that exact list.
#
# Usage:
#   pd_worklist.sh "<Field name | Field id | 4-digit Subfield id | T-prefixed Topic id | free-text query>" \
#     [--limit 30] [--year-from YYYY] [--similar 2] [--out DIR] [--no-synth]
#   pd_worklist.sh --paper <paper id | DOI | arXiv id> \
#     [--limit 30] [--out DIR] [--no-synth]
#
# Retrieval follows the layered protocol (docs/api/AGENT_PROTOCOL.md §2):
# facet pool-pulls stay on GET /papers?…_id=, fuzzy text goes to
# GET /papers/search?q=&mode=auto (title→semantic waterfall, 0.8.0+), exact
# identifiers go to GET /papers/resolve?id=. POST /ask is used exactly once,
# for overview.md — never as a retrieval channel.
#
# The first positional arg resolves by shape:
#   - T-prefixed id (e.g. T10270)           → Topic
#   - 1-2 digit id (e.g. 17)                → Field
#   - 4-digit id (e.g. 1702)                → Subfield
#   - "Field name" / "Subfield name" exact  → Field / Subfield by name
#   - other string                          → Topic-name scan across all
#                                              Subfields (~3-5s, parallel walk);
#                                              still no hit → free-text query via
#                                              GET /papers/search?q=&mode=auto
#
# --paper single-seed mode: GET /papers/resolve?id= resolves the seed (404 →
# error with a /papers/search hint), then GET /papers/{id}/similar?k=<limit>
# expands it into the worklist (seed is the first row; --similar is ignored).
#
# Examples:
#   pd_worklist.sh "Artificial Intelligence"
#   pd_worklist.sh 1702 --limit 20 --year-from 2025
#   pd_worklist.sh T10270 --similar 3 --out ./research/blockchain
#   pd_worklist.sh "graph neural networks" --no-synth
#   pd_worklist.sh --paper arxiv:2605.10419
#   pd_worklist.sh --paper 10.2139/ssrn.7123198 --limit 20
#
# Output ($OUT, default ./pd-research/<slug of resolved target>/):
#   worklist.jsonl      — one JSON object per line: id, title, publication_date,
#                          venue, doi, arxiv_id, oa_url, source
#                          ("primary"|"similar"|"seed")
#   worklist.meta.json  — provenance sidecar: taxonomy target + created_at
#                          (consumed by Stage 4 upload_session.py)
#   overview.md         — LLM synthesis grounded in the worklist via
#                          load_extractions(paper_ids=[...]) (skipped by --no-synth)
#
# Auth: sourced from ~/.paperdaily-cli/env (must export PD_BASE + PD_KEY).
# If missing: issue a key from the paperdaily web UI, Settings → API keys.
#
# Scopes needed: read:paper (resolve / search / list / similar / detail),
# synth:ask (unless --no-synth). Free tier read:paper is 60 req/min — this
# script paces GET calls ~0.15s apart and retries once on 429, but a large
# --limit x --similar combo can still legitimately exceed the per-minute cap;
# a handful of warned skips is expected, not a bug.
#
# Self-contained: only bash + curl + jq. bash 3.2 (macOS default) compatible.

set -euo pipefail

command -v curl >/dev/null 2>&1 || { echo "pd_worklist.sh: curl not found" >&2; exit 1; }
command -v jq   >/dev/null 2>&1 || { echo "pd_worklist.sh: jq not found (brew install jq / apt install jq)" >&2; exit 1; }

# ── 0. auth ───────────────────────────────────────────────────────────
if [[ -z "${PD_KEY:-}" || -z "${PD_BASE:-}" ]]; then
  if [[ -r "$HOME/.paperdaily-cli/env" ]]; then
    # shellcheck disable=SC1090
    source "$HOME/.paperdaily-cli/env"
  fi
fi
if [[ -z "${PD_KEY:-}" || -z "${PD_BASE:-}" ]]; then
  cat >&2 <<'EOF'
pd_worklist.sh: missing PD_BASE / PD_KEY.

Create ~/.paperdaily-cli/env with:
  export PD_BASE="https://www.paperdaily.org/api/v1"
  export PD_KEY="pd_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

No key yet? Issue one from the paperdaily web UI:
  Settings → API keys → Issue key   (scopes: read:paper, synth:ask)
EOF
  exit 1
fi

# ── 1. arg parsing ───────────────────────────────────────────────────
LIMIT=30
YEAR_FROM=""
YEAR_FROM_SET=0
SIMILAR=2
SIMILAR_SET=0
OUT=""
DO_SYNTH=1
SEED_PAPER=""
target_raw=""

print_help() { sed -n '2,59p' "$0"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit)      LIMIT=$2; shift 2 ;;
    --year-from)  YEAR_FROM=$2; YEAR_FROM_SET=1; shift 2 ;;
    --similar)    SIMILAR=$2; SIMILAR_SET=1; shift 2 ;;
    --paper)      SEED_PAPER=$2; shift 2 ;;
    --out)        OUT=$2; shift 2 ;;
    --no-synth)   DO_SYNTH=0; shift ;;
    --help|-h)    print_help; exit 0 ;;
    -*)           echo "pd_worklist.sh: unknown flag: $1" >&2; exit 2 ;;
    *)            target_raw="${target_raw}${target_raw:+ }$1"; shift ;;
  esac
done
if [[ -n "$SEED_PAPER" && -n "$target_raw" ]]; then
  echo "pd_worklist.sh: --paper and a positional target are mutually exclusive" >&2; exit 2
fi
if [[ -z "$SEED_PAPER" && -z "$target_raw" ]]; then
  echo "usage: pd_worklist.sh <field-or-subfield-or-topic-or-query> [flags]  |  pd_worklist.sh --paper <id> [flags]" >&2; exit 2
fi

# ── 2. tiny HTTP client (body+status in one request; retry once on 429) ──
pd_api() {
  # pd_api METHOD PATH [BODY]  -> prints response body to stdout.
  # Returns 0 on 2xx, 1 otherwise (error detail goes to stderr).
  local method="$1" path="$2" body="${3:-}"
  local url="${PD_BASE}${path}"
  local raw code out attempt

  for attempt in 1 2; do
    if [[ -n "$body" ]]; then
      raw=$(curl -sS -w $'\n%{http_code}' \
        -H "Authorization: Bearer $PD_KEY" -H 'Content-Type: application/json' \
        -X "$method" -d "$body" "$url")
    else
      raw=$(curl -sS -w $'\n%{http_code}' \
        -H "Authorization: Bearer $PD_KEY" -X "$method" "$url")
    fi
    code="${raw##*$'\n'}"
    out="${raw%$'\n'*}"
    if [[ "$code" == "429" && "$attempt" == "1" ]]; then
      echo "pd_worklist.sh: 429 on $method $path — backing off 3s, retrying once" >&2
      sleep 3
      continue
    fi
    break
  done

  if [[ "$code" -ge 400 ]]; then
    echo "pd_worklist.sh: $method $path -> HTTP $code: $out" >&2
    return 1
  fi
  printf '%s' "$out"
  return 0
}

slugify() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g' | cut -c1-60
}

urlenc() {
  jq -rn --arg s "$1" '$s|@uri'
}

# ── 3. taxonomy resolution (T-id / numeric id / exact name / substring /
#      topic-name scan — same tiers as the internal skill's field-digest.sh) ──

# Parallel scan of Topic display_names across all Subfields. Self-contained
# reimplementation of the internal skill's topic-find.sh (can't source it —
# this script ships standalone). Prints the single best hit (highest
# paper_count) as a TSV line: id\tpaper_count\tsubfield_id\tdisplay_name
_pd_scan_subfield_topics() {
  local sf="$1"
  curl -sS -H "Authorization: Bearer $PD_KEY" \
    "$PD_BASE/taxonomy/topics?subfield_id=$sf&limit=200" 2>/dev/null \
    | jq -r --arg sf "$sf" --arg n "$PD_NEEDLE_LC" '
        if type == "array" then
          .[] | select(.display_name | ascii_downcase | contains($n))
              | [.id, .paper_count, $sf, .display_name] | @tsv
        else empty end' 2>/dev/null
}

topic_find_by_name() {
  local needle_lc="$1" sf_ids
  sf_ids=$(pd_api GET "/taxonomy/subfields") || return 1
  sf_ids=$(printf '%s' "$sf_ids" | jq -r '.[].id')

  export -f _pd_scan_subfield_topics
  export PD_BASE PD_KEY
  export PD_NEEDLE_LC="$needle_lc"

  printf '%s\n' "$sf_ids" | xargs -P 4 -I {} bash -c '_pd_scan_subfield_topics "$@"' _ {} \
    | sort -t $'\t' -k2 -nr | head -1
}

resolve_taxon() {
  local raw="$1"
  if [[ "$raw" =~ ^T[0-9]+$ ]]; then echo "topic|$raw|"; return 0; fi
  if [[ "$raw" =~ ^[0-9]+$ ]]; then
    if [[ ${#raw} -le 2 ]]; then echo "field|$raw|"; return 0; fi
    if [[ ${#raw} -eq 4 ]]; then echo "subfield|$raw|"; return 0; fi
  fi

  local fields subfields hit
  fields=$(pd_api GET "/taxonomy/fields") || return 1
  hit=$(printf '%s' "$fields" | jq -r --arg q "$raw" \
    '[.[] | select(.display_name | ascii_downcase == ($q|ascii_downcase))][0] // empty | "field|" + .id + "|" + .display_name')
  if [[ -n "$hit" ]]; then echo "$hit"; return 0; fi

  subfields=$(pd_api GET "/taxonomy/subfields") || return 1
  hit=$(printf '%s' "$subfields" | jq -r --arg q "$raw" \
    '[.[] | select(.display_name | ascii_downcase == ($q|ascii_downcase))][0] // empty | "subfield|" + .id + "|" + .display_name')
  if [[ -n "$hit" ]]; then echo "$hit"; return 0; fi

  hit=$(printf '%s' "$subfields" | jq -r --arg q "$raw" \
    '[.[] | select(.display_name | ascii_downcase | contains($q|ascii_downcase))] | sort_by(-.paper_count) | .[0] // empty | "subfield|" + .id + "|" + .display_name')
  if [[ -n "$hit" ]]; then echo "$hit"; return 0; fi

  echo "pd_worklist.sh: resolving '$raw' as a Topic name — scanning subfields (~3-5s) …" >&2
  local needle_lc topic_hit tid tname
  needle_lc=$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')
  topic_hit=$(topic_find_by_name "$needle_lc") || topic_hit=""
  if [[ -n "$topic_hit" ]]; then
    tid=$(printf '%s' "$topic_hit" | cut -f1)
    tname=$(printf '%s' "$topic_hit" | cut -f4)
    echo "topic|$tid|$tname"
    return 0
  fi
  # No taxonomy hit at all — treat as a free-text query for the layered
  # search endpoint (GET /papers/search?q=&mode=auto, 0.8.0+).
  echo "pd_worklist.sh: no taxonomy match for '$raw' — falling back to /papers/search free-text query" >&2
  echo "query|$raw|$raw"
  return 0
}

if [[ -n "$SEED_PAPER" ]]; then
  # ── single-seed mode: resolve the identifier, expand via /similar ──
  # loud no-op warnings for flags that only apply to taxonomy/query targets
  # (behavior unchanged — they are simply not used on this path)
  if [[ "$YEAR_FROM_SET" == "1" ]]; then
    echo "pd_worklist.sh: WARNING: --year-from has no effect in --paper seed mode (the /similar expansion is not year-filtered) — ignored" >&2
  fi
  if [[ "$SIMILAR_SET" == "1" ]]; then
    echo "pd_worklist.sh: WARNING: --similar has no effect in --paper seed mode (--limit sizes the single /similar?k= expansion instead) — ignored" >&2
  fi
  seed_json=""
  if ! seed_json=$(pd_api GET "/papers/resolve?id=$(urlenc "$SEED_PAPER")"); then
    cat >&2 <<EOF
pd_worklist.sh: could not resolve seed paper '$SEED_PAPER' (see HTTP error above).
A 404 means the paper is not in the corpus. Try locating it first:
  GET \$PD_BASE/papers/search?q=<title words>&mode=auto
or double-check the identifier (W-id / DOI / arXiv id all accepted).
EOF
    exit 3
  fi
  LEVEL="paper"
  SEED_ID=$(printf '%s' "$seed_json" | jq -r '.id')
  TAX_ID="$SEED_ID"
  TAX_NAME=$(printf '%s' "$seed_json" | jq -r '.title // .id')
  seed_resolved_by=$(printf '%s' "$seed_json" | jq -r '.resolved_by // "id"')
  echo "pd_worklist.sh: seed resolved ($seed_resolved_by): $SEED_ID — $TAX_NAME" >&2
else
  RESOLVED=$(resolve_taxon "$target_raw")
  [[ -n "$RESOLVED" ]] || { echo "pd_worklist.sh: could not resolve '$target_raw' to a Field/Subfield/Topic/query" >&2; exit 3; }
  IFS='|' read -r LEVEL TAX_ID TAX_NAME <<<"$RESOLVED"
fi

# Numeric / T-id paths returned no display_name above — fetch one for
# human-readable output + the default --out slug.
if [[ -z "$TAX_NAME" ]]; then
  case "$LEVEL" in
    field)
      TAX_NAME=$(pd_api GET "/taxonomy/fields" \
        | jq -r --arg id "$TAX_ID" '[.[] | select(.id==$id)][0].display_name // $id') ;;
    subfield)
      TAX_NAME=$(pd_api GET "/taxonomy/subfields" \
        | jq -r --arg id "$TAX_ID" '[.[] | select(.id==$id)][0].display_name // $id') ;;
    topic)
      # Global topic listing caps at limit=500; a long-tail Topic outside
      # the top-500 by paper_count just falls back to showing its id.
      TAX_NAME=$(pd_api GET "/taxonomy/topics?limit=500" \
        | jq -r --arg id "$TAX_ID" 'if type=="array" then [.[] | select(.id==$id)][0].display_name // $id else $id end') ;;
  esac
fi
TAX_NAME=${TAX_NAME:-$TAX_ID}

[[ -n "$OUT" ]] || OUT="./pd-research/$(slugify "$TAX_NAME")"
OUT="${OUT%/}"
mkdir -p "$OUT"

# ── 4. primary list: facet pool-pull, layered search, or seed resolve ──
if [[ "$LEVEL" == "paper" ]]; then
  n_primary=1                                   # the resolved seed itself
elif [[ "$LEVEL" == "query" ]]; then
  # Free-text query → layered search endpoint (0.8.0+): identifier→title→
  # semantic waterfall server-side; items carry match_layer + score.
  papers_q="/papers/search?q=$(urlenc "$TAX_ID")&mode=auto&limit=${LIMIT}"
  [[ -n "$YEAR_FROM" ]] && papers_q="${papers_q}&year_from=${YEAR_FROM}"
  if ! primary_json=$(pd_api GET "$papers_q"); then
    echo "pd_worklist.sh: /papers/search failed — a 404 here means the server predates 0.8.0 (no layered search); use a taxonomy name/id target instead" >&2
    exit 4
  fi
  layer_used=$(printf '%s' "$primary_json" | jq -r '.layer_used // "?"')
  echo "pd_worklist.sh: /papers/search layer_used=$layer_used" >&2
  n_primary=$(printf '%s' "$primary_json" | jq '.items | length')
  if [[ "$n_primary" == "0" ]]; then
    echo "pd_worklist.sh: no papers matched query '$TAX_ID' (try fewer/other words, or a taxonomy target)" >&2
    exit 0
  fi
else
  papers_q="/papers?${LEVEL}_id=${TAX_ID}&has_extraction=true&limit=${LIMIT}"
  [[ -n "$YEAR_FROM" ]] && papers_q="${papers_q}&year_from=${YEAR_FROM}"

  primary_json=$(pd_api GET "$papers_q") || { echo "pd_worklist.sh: failed to fetch primary paper list" >&2; exit 4; }
  n_primary=$(printf '%s' "$primary_json" | jq '.items | length')

  if [[ "$n_primary" == "0" ]]; then
    echo "pd_worklist.sh: no papers matched ${LEVEL}_id=${TAX_ID} (try relaxing --year-from)" >&2
    exit 0
  fi
fi

# ── 5. merge primary + similar-neighbour expansion, deduped, ordered ───
ids_only_file=$(mktemp "${TMPDIR:-/tmp}/pd_worklist_ids.XXXXXX")
ids_tsv_file=$(mktemp "${TMPDIR:-/tmp}/pd_worklist_tsv.XXXXXX")
trap 'rm -f "$ids_only_file" "$ids_tsv_file"' EXIT

add_id() {
  local id="$1" src="$2"
  [[ -n "$id" && "$id" != "null" ]] || return 0
  if grep -qxF "$id" "$ids_only_file" 2>/dev/null; then
    return 0
  fi
  printf '%s\n' "$id" >> "$ids_only_file"
  printf '%s\t%s\n' "$id" "$src" >> "$ids_tsv_file"
}

if [[ "$LEVEL" == "paper" ]]; then
  # seed first row, then one /similar expansion sized by --limit
  add_id "$SEED_ID" "seed"
  n_primary_added=1
  sim_json=""
  if ! sim_json=$(pd_api GET "/papers/${SEED_ID}/similar?k=${LIMIT}"); then
    echo "pd_worklist.sh: warning: similar-fetch failed for seed $SEED_ID — worklist will be the seed alone" >&2
  else
    while IFS= read -r sid; do
      [[ -n "$sid" ]] && add_id "$sid" "similar"
    done < <(printf '%s' "$sim_json" | jq -r '.items[].paper.id')
  fi
else
  primary_ids=$(printf '%s' "$primary_json" | jq -r '.items[].id')
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && add_id "$pid" "primary"
  done <<<"$primary_ids"
  n_primary_added=$(wc -l < "$ids_only_file" | tr -d ' ')

  if [[ "$SIMILAR" -gt 0 ]]; then
    while IFS= read -r pid; do
      [[ -n "$pid" ]] || continue
      sim_json=""
      if ! sim_json=$(pd_api GET "/papers/${pid}/similar?k=${SIMILAR}"); then
        echo "pd_worklist.sh: warning: similar-fetch failed for $pid, skipping expansion" >&2
      else
        while IFS= read -r sid; do
          [[ -n "$sid" ]] && add_id "$sid" "similar"
        done < <(printf '%s' "$sim_json" | jq -r '.items[].paper.id')
      fi
      sleep 0.15
    done <<<"$primary_ids"
  fi
fi

n_merged=$(wc -l < "$ids_only_file" | tr -d ' ')

# ── 6. per-paper detail -> worklist.jsonl ───────────────────────────
: > "$OUT/worklist.jsonl"
n_written=0
while IFS=$'\t' read -r pid src; do
  [[ -n "$pid" ]] || continue
  detail_json=""
  if ! detail_json=$(pd_api GET "/papers/${pid}"); then
    echo "pd_worklist.sh: warning: detail-fetch failed for $pid, skipping" >&2
  else
    # paperdaily's paper_etl writes several missing string attrs as "" not
    # JSON null (known gotcha — see repo memory empty_string_in_attrs);
    # normalize "" -> null here so the worklist honors "null not empty string".
    printf '%s' "$detail_json" | jq -c --arg source "$src" '
      def blank_to_null: if . == "" or . == null then null else . end;
      {
        id: .id,
        title: .title,
        publication_date: (.publication_date | blank_to_null),
        venue: (.venue_name | blank_to_null),
        doi: (.doi | blank_to_null),
        arxiv_id: (.arxiv_id | blank_to_null),
        oa_url: (.external_full_text_url | blank_to_null),
        source: $source
      }' >> "$OUT/worklist.jsonl"
    n_written=$((n_written + 1))
  fi
  sleep 0.15
done < "$ids_tsv_file"

# Provenance sidecar for Stage 4 (upload_session.py reads .taxonomy /
# .taxon_name). Extension file per SKILL_SPEC §8 — core artifact names unchanged.
jq -nc --arg level "$LEVEL" --arg id "$TAX_ID" --arg name "$TAX_NAME" \
  '{taxonomy: ($level + ":" + $id), taxon_name: $name, created_at: (now | todate)}' \
  > "$OUT/worklist.meta.json"

# ── 7. optional LLM synthesis over the worklist ─────────────────────
if [[ "$DO_SYNTH" == "1" ]]; then
  synth_ids=$(jq -r '.id' "$OUT/worklist.jsonl" | head -15 | paste -sd, -)
  if [[ -z "$synth_ids" ]]; then
    echo "pd_worklist.sh: nothing to synthesize (empty worklist), skipping /ask" >&2
  else
    question=$(cat <<EOF
Use the tool \`load_extractions(paper_ids=[$synth_ids])\` to fetch the
abstracts and extractions of these specific papers (do NOT call
find_papers_by_keyword_semantic — the papers are already selected by id).

Then write a research overview of "$TAX_NAME" covering:
1. What 2-4 emerging research threads do these papers represent?
2. Which 2-3 papers are the strongest starting points for a literature
   review, and why?
3. Any notable methodological or dataset trends across these papers.

If a paper has no cached extraction, work from its title/abstract alone
and say so. Cite each paper by its id (e.g. \`arxiv:2605.21488\` or
\`W1234567890\`) when discussing it.
EOF
)
    body=$(jq -nc --arg q "$question" '{question:$q, max_iters:4}')
    ask_resp=""
    if ! ask_resp=$(pd_api POST "/ask" "$body"); then
      echo "pd_worklist.sh: /ask failed, skipping overview.md" >&2
    else
      {
        echo "# Overview — $TAX_NAME"
        echo
        echo "_tokens_in=$(printf '%s' "$ask_resp" | jq -r '.tokens_in // "?"'), tokens_out=$(printf '%s' "$ask_resp" | jq -r '.tokens_out // "?"'), cited=$(printf '%s' "$ask_resp" | jq -r '.cited_paper_ids | length')_"
        echo
        printf '%s' "$ask_resp" | jq -r '.answer // "(no answer returned)"'
      } > "$OUT/overview.md"
    fi
  fi
fi

# ── 8. summary ───────────────────────────────────────────────────────
echo
echo "== pd_worklist summary =="
case "$LEVEL" in
  paper) echo "seed paper: $TAX_ID ($TAX_NAME)" ;;
  query) echo "free-text query: $TAX_ID (layer_used=${layer_used:-?})" ;;
  *)     echo "taxonomy: $LEVEL=$TAX_ID ($TAX_NAME)" ;;
esac
echo "primary papers: $n_primary_added   similar-expanded: $((n_merged - n_primary_added))   merged unique: $n_merged"
echo "worklist rows written: $n_written"
echo "worklist: $OUT/worklist.jsonl"
if [[ "$DO_SYNTH" == "1" && -f "$OUT/overview.md" ]]; then
  echo "overview: $OUT/overview.md"
fi
