#!/usr/bin/env bash
# pd_worklist.sh — Stage 1 of the paperdaily deep-research skill: resolve a
# target (taxonomy node / free-text query / single seed paper), pull a paper
# worklist (+ similar-paper expansion), and optionally synthesize a short LLM
# overview grounded in that exact list.
#
# Usage:
#   pd_worklist.sh "<Field name | Field id | 4-digit Subfield id | T-prefixed Topic id | free-text query>" \
#     [--limit 25] [--year-from YYYY] [--similar 2] [--out DIR] [--no-synth] \
#     [--throttle 1.05] [--deep-topic-scan] [--append] [--search-mode semantic|title|auto]
#   pd_worklist.sh --paper <paper id | DOI | arXiv id> \
#     [--limit 25] [--out DIR] [--no-synth]
#
# Retrieval follows the layered protocol (docs/api/AGENT_PROTOCOL.md §2):
# facet pool-pulls stay on GET /papers?…_id=, fuzzy text goes to
# GET /papers/search?q=&mode=semantic (pinned — NOT the mode=auto waterfall,
# whose title layer is accepted at >=3 hits and starves the vector layer; see
# the SEARCH_MODE comment below for the measurements), exact identifiers go to
# GET /papers/resolve?id=. POST /ask is used exactly once, for overview.md —
# never as a retrieval channel.
#
# The first positional arg resolves by shape:
#   - T-prefixed id (e.g. T10270)           → Topic
#   - 1-2 digit id (e.g. 17)                → Field
#   - 4-digit id (e.g. 1702)                → Subfield
#   - "Field name" / "Subfield name" exact  → Field / Subfield by name
#   - other string                          → Topic-name match against the
#                                              global top-500 Topics by paper
#                                              count (ONE request); still no hit
#                                              → free-text query via
#                                              GET /papers/search?q=&mode=semantic
#                                              (override: --search-mode title|auto)
#     (--deep-topic-scan forces an exhaustive per-Subfield walk for long-tail
#      Topics outside the top-500: ~250 sequential requests, several minutes,
#      and it eats the whole read:paper quota. Opt-in for a reason.)
#
# --paper single-seed mode: GET /papers/resolve?id= resolves the seed (404 →
# error with a /papers/search hint), then GET /papers/{id}/similar?k=<limit>
# expands it into the worklist (seed is the first row; --similar is ignored).
# NOTE the off-by-one, and that it compounds with twin collapse: --limit sizes
# the /similar expansion ONLY, so the raw pool is up to limit+1 rows (seed +
# limit neighbours) — and the collapse pass then REMOVES the twins, typically
# 20-30% of them on this corpus. Want N distinct works? Over-request: start
# around --limit $((N * 3 / 2)) and read the "worklist rows written" line, which
# is the post-collapse count.
#
# Examples:
#   pd_worklist.sh "Artificial Intelligence"
#   pd_worklist.sh 1702 --limit 20 --year-from 2025
#   pd_worklist.sh T10270 --similar 2 --out ./research/blockchain
#   pd_worklist.sh "graph neural networks" --no-synth
#   pd_worklist.sh --paper arxiv:2605.10419
#   pd_worklist.sh --paper 10.2139/ssrn.7123198 --limit 20
#
# Output ($OUT, default ./pd-research/<slug of resolved target>/):
#   worklist.jsonl      — one JSON object per line: id, title, publication_date,
#                          venue, doi, arxiv_id, oa_url, source
#                          ("primary"|"similar"|"seed"), twins collapsed
#   worklist.twins.jsonl— audit trail: one line per collapsed twin
#                          {kind: "hard"|"title", kept, dropped, title}
#   worklist.meta.json  — provenance sidecar: taxonomy target + created_at
#                          (consumed by Stage 4 upload_session.py)
#   overview.md         — LLM synthesis grounded in the worklist via
#                          load_extractions(paper_ids=[...]) (skipped by --no-synth)
#
# Auth: sourced from ~/.paperdaily-cli/env (must export PD_BASE + PD_KEY).
# If missing: issue a key from the paperdaily web UI, Settings → API keys.
#
# Scopes needed: read:paper (resolve / search / list / similar / detail),
# synth:ask (unless --no-synth).
#
# Rate limiting — free tier read:paper is 60 req/min in a FIXED 60s window
# (api/middleware/api_key_auth.py). Every GET therefore self-paces at
# --throttle seconds (default 1.05 ≈ 57/min, i.e. below the cap by design),
# and a 429 is retried up to 3 times honouring the server's Retry-After.
# An earlier version paced at 0.15s (≈400/min): it blew the quota on any
# default run and then dropped papers with a one-line warning, quietly
# shrinking the worklist. Papers that still fail to fetch are now counted
# and reported as an INCOMPLETE banner — a short worklist must never look
# like a small field.
#
# Wall clock: pacing is the dominant cost. Budget roughly
# (2 + n_primary + n_merged) x throttle seconds — e.g. ~80s at the defaults.
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
  export PD_BASE="http://<your-paperdaily-host>/api/v1"
  export PD_KEY="pd_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

No key yet? Issue one from the paperdaily web UI:
  Settings → API keys → Issue key   (scopes: read:paper, synth:ask)
EOF
  exit 1
fi

# ── 1. arg parsing ───────────────────────────────────────────────────
# Defaults size a *candidate pool for triage*, not a reading list: 25 primary
# x 2 similar neighbours, minus twin collapse, lands around 35-50 distinct
# works — enough for a human to skim down to the 8-15 that actually get
# deep-read (SKILL.md Stage 1.5). The pre-0.4.0 30 x 2 defaults produced pools
# of up to 90 rows, i.e. 90 PDF downloads and 90 reading sub-agents if taken
# literally, which is nobody's idea of a deep read.
LIMIT=25
YEAR_FROM=""
YEAR_FROM_SET=0
# --similar must stay >= 2 on this corpus. At k=1 a paper's single nearest
# neighbour is usually its own twin vertex, which the pool already holds, so
# the expansion silently contributes nothing ("similar-expanded: 0", measured).
# Raising k costs no extra REQUESTS — one /similar call per primary either way,
# only the returned list is longer — so there is no reason to run it at 1.
SIMILAR=2
SIMILAR_SET=0
# Free-text targets pin the SEMANTIC layer. `mode=auto` runs the trgm title
# layer first and ACCEPTS it at >=3 hits, so a research direction routinely
# comes back as three literal title matches and the vector layer never runs —
# measured 2026-08-18 on prod: "mixture of experts routing" auto = 19.6s / 3
# rows vs pinned semantic = 5.1s / 20 rows; "causal inference with panel data
# staggered adoption" auto = 23.6s vs 1.5s. That is both the thin-pool failure
# this script used to warn about AND most of the "search is slow" complaint.
# Override with --search-mode when the target really is a literal title.
SEARCH_MODE=semantic
APPEND=0
OUT=""
DO_SYNTH=1
SEED_PAPER=""
THROTTLE="${PD_THROTTLE:-1.05}"
DEEP_TOPIC_SCAN=0
target_raw=""

print_help() { sed -n '2,85p' "$0"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit)      LIMIT=$2; shift 2 ;;
    --year-from)  YEAR_FROM=$2; YEAR_FROM_SET=1; shift 2 ;;
    --similar)    SIMILAR=$2; SIMILAR_SET=1; shift 2 ;;
    --search-mode)
      case "$2" in
        semantic|title|auto) SEARCH_MODE=$2 ;;
        *) echo "pd_worklist.sh: --search-mode must be semantic|title|auto" >&2; exit 2 ;;
      esac
      shift 2 ;;
    --paper)      SEED_PAPER=$2; shift 2 ;;
    --out)        OUT=$2; shift 2 ;;
    --throttle)   THROTTLE=$2; shift 2 ;;
    --append)     APPEND=1; shift ;;
    --deep-topic-scan) DEEP_TOPIC_SCAN=1; shift ;;
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

# ── 2. tiny HTTP client (self-paced; 429 → Retry-After backoff, ≤3 tries) ──
# Every call sleeps $THROTTLE after the response. Command substitution puts
# pd_api in a subshell, so a "last request time" variable would not survive
# between calls — sleeping after each request is the one pacing scheme that
# works regardless, at the cost of adding the request latency on top.
_HDR_FILE=$(mktemp "${TMPDIR:-/tmp}/pd_worklist_hdr.XXXXXX")
N_RETRY_WAITS=0   # how many times we sat out a 429 (reported in the summary)

_retry_after_secs() {
  # Server sends Retry-After = seconds left in the fixed 60s window. Honour it
  # (a blind 3s backoff retries straight into the same closed window), but cap
  # it so a misconfigured server cannot park the run forever.
  local v
  v=$(tr -d '\r' < "$_HDR_FILE" 2>/dev/null | awk 'tolower($1)=="retry-after:"{print $2}' | tail -1)
  case "$v" in ''|*[!0-9]*) v=5 ;; esac
  [[ "$v" -gt 65 ]] && v=65
  [[ "$v" -lt 1 ]] && v=1
  printf '%s' "$v"
}

pd_api() {
  # pd_api METHOD PATH [BODY]  -> prints response body to stdout.
  # Returns 0 on 2xx, 1 otherwise (error detail goes to stderr).
  local method="$1" path="$2" body="${3:-}"
  local url="${PD_BASE}${path}"
  local raw code out attempt wait_s

  for attempt in 1 2 3; do
    if [[ -n "$body" ]]; then
      raw=$(curl -sS -D "$_HDR_FILE" -w $'\n%{http_code}' \
        -H "Authorization: Bearer $PD_KEY" -H 'Content-Type: application/json' \
        -X "$method" -d "$body" "$url")
    else
      raw=$(curl -sS -D "$_HDR_FILE" -w $'\n%{http_code}' \
        -H "Authorization: Bearer $PD_KEY" -X "$method" "$url")
    fi
    code="${raw##*$'\n'}"
    out="${raw%$'\n'*}"
    sleep "$THROTTLE"
    if [[ "$code" == "429" && "$attempt" -lt 3 ]]; then
      wait_s=$(_retry_after_secs)
      echo "pd_worklist.sh: 429 on $method $path — quota window closed, waiting ${wait_s}s (attempt $attempt/3)" >&2
      N_RETRY_WAITS=$((N_RETRY_WAITS + 1))
      sleep "$wait_s"
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

# jq that must not take down the script: an unexpected response shape (an SPA
# HTML fallback, an error envelope) should read as a clear message, not as a
# `set -e` abort inside a command substitution.
json_len() {
  # json_len <json> <path>  -> array length, or "" when the shape is wrong
  printf '%s' "$1" | jq -r --arg p "$2" 'getpath($p | split(".")) | if type=="array" then length else empty end' 2>/dev/null
}

slugify() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g' | cut -c1-60
}

urlenc() {
  jq -rn --arg s "$1" '$s|@uri'
}

# ── 3. taxonomy resolution (T-id / numeric id / exact name / substring /
#      topic-name scan — same tiers as the internal skill's field-digest.sh) ──

# Topic-name resolution in two tiers. Prints the best hit (highest
# paper_count) as a TSV line: id\tpaper_count\tsubfield_id\tdisplay_name
#
# Tier 1 is ONE request against the global Topic listing, which the server
# returns ordered by paper_count (max limit=500). Tier 2 walks every Subfield
# and only exists for long-tail Topics below that cut.
#
# Tier 2 used to be the *only* tier, and ran as `xargs -P 4` over ~250
# Subfields with bare curl: it bypassed the retry path, fired ~250 requests
# into a 60 req/min quota, and swallowed every error with 2>/dev/null — so a
# throttled scan was indistinguishable from "this Topic does not exist" and
# silently degraded into a semantic search. Now it is sequential, paced,
# loud about failures, and opt-in.
_topic_pick_from_list() {
  # _topic_pick_from_list <json-array> <needle_lc> [subfield_id]
  printf '%s' "$1" | jq -r --arg n "$2" --arg sf "${3:-}" '
      if type == "array" then
        [.[] | select(.display_name | ascii_downcase | contains($n))]
        | sort_by(-.paper_count) | .[0] // empty
        | [.id, .paper_count, (if $sf == "" then (.parent_id // "") else $sf end), .display_name]
        | @tsv
      else empty end' 2>/dev/null
}

topic_find_by_name() {
  local needle_lc="$1" hit topics sf_ids sf n_scanned n_failed best

  # ── tier 1: global top-500 by paper_count, one request ──
  topics=$(pd_api GET "/taxonomy/topics?limit=500") || return 1
  hit=$(_topic_pick_from_list "$topics" "$needle_lc")
  if [[ -n "$hit" ]]; then printf '%s' "$hit"; return 0; fi

  if [[ "$DEEP_TOPIC_SCAN" != "1" ]]; then
    cat >&2 <<EOF
pd_worklist.sh: '$needle_lc' is not among the 500 largest Topics (that cut is by
  GLOBAL paper count, so even a well-known CS/AI topic can sit below it).
  Skipping the exhaustive per-Subfield scan — ~250 sequential requests, several
  minutes, and it spends the whole read:paper minute quota. Falling through to
  free-text /papers/search, which usually finds these papers anyway.
  If you specifically need a Topic id for a facet pool-pull: pass a Subfield id
  instead (4 digits, e.g. 1702), or force the walk with --deep-topic-scan.
EOF
    return 1
  fi

  # ── tier 2: exhaustive, sequential, paced, honest about failures ──
  sf_ids=$(pd_api GET "/taxonomy/subfields") || return 1
  sf_ids=$(printf '%s' "$sf_ids" | jq -r '.[].id')
  local n_total
  n_total=$(printf '%s\n' "$sf_ids" | grep -c . || true)
  echo "pd_worklist.sh: --deep-topic-scan: walking $n_total subfields at ${THROTTLE}s/request — expect ~$(( n_total * 2 )) seconds …" >&2
  n_scanned=0; n_failed=0; best=""
  while IFS= read -r sf; do
    [[ -n "$sf" ]] || continue
    local page row
    if ! page=$(pd_api GET "/taxonomy/topics?subfield_id=$sf&limit=200"); then
      n_failed=$((n_failed + 1))
      continue
    fi
    n_scanned=$((n_scanned + 1))
    row=$(_topic_pick_from_list "$page" "$needle_lc" "$sf")
    if [[ -n "$row" ]]; then
      if [[ -z "$best" ]] || \
         [[ "$(printf '%s' "$row" | cut -f2)" -gt "$(printf '%s' "$best" | cut -f2)" ]]; then
        best="$row"
      fi
    fi
  done <<<"$sf_ids"
  if [[ "$n_failed" -gt 0 ]]; then
    echo "pd_worklist.sh: WARNING: deep topic scan covered $n_scanned/$n_total subfields ($n_failed failed) — a 'no match' below may just be the unscanned part" >&2
  fi
  [[ -n "$best" ]] || return 1
  printf '%s' "$best"
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

  echo "pd_worklist.sh: resolving '$raw' as a Topic name …" >&2
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
  # search endpoint (GET /papers/search?q=&mode=semantic by default, 0.8.0+).
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
  echo "pd_worklist.sh: seed mode — --limit ${LIMIT} sizes the neighbour expansion only, so the pool will be up to $((LIMIT + 1)) rows (seed + ${LIMIT}) before twin collapse" >&2
  seed_json=""
  if ! seed_json=$(pd_api GET "/papers/resolve?id=$(urlenc "$SEED_PAPER")"); then
    cat >&2 <<EOF
pd_worklist.sh: could not resolve seed paper '$SEED_PAPER' (see HTTP error above).
A 404 means the paper is not in the corpus. Try locating it first:
  GET \$PD_BASE/papers/search?q=<title words>&mode=title
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
  papers_q="/papers/search?q=$(urlenc "$TAX_ID")&mode=${SEARCH_MODE}&limit=${LIMIT}"
  nwords=$(printf '%s' "$TAX_ID" | wc -w | tr -d ' ')
  if [[ "$SEARCH_MODE" == "semantic" && "$nwords" -lt 4 ]]; then
    cat >&2 <<EOF
pd_worklist.sh: NOTE: "$TAX_ID" is $nwords word(s). The semantic layer reranks by
  dominant-topic consensus, and a keyword-length query drags the whole batch into
  the wrong cluster while still looking healthy (measured: "chain-of-thought
  prompting" -> Educational-Leadership editorials at score 0.542; the same idea as
  a full sentence put the real paper first at 0.759). Describe the direction in a
  sentence, and check the top score / the why[] cluster below before trusting it.
EOF
  fi
  [[ -n "$YEAR_FROM" ]] && papers_q="${papers_q}&year_from=${YEAR_FROM}"
  if ! primary_json=$(pd_api GET "$papers_q"); then
    echo "pd_worklist.sh: /papers/search failed — a 404 here means the server predates 0.8.0 (no layered search); use a taxonomy name/id target instead" >&2
    exit 4
  fi
  layer_used=$(printf '%s' "$primary_json" | jq -r '.layer_used // "?"' 2>/dev/null || echo "?")
  echo "pd_worklist.sh: /papers/search layer_used=$layer_used" >&2
  n_primary=$(json_len "$primary_json" "items")
  if [[ -z "$n_primary" ]]; then
    echo "pd_worklist.sh: /papers/search returned no .items array — unexpected response shape. First 300 bytes:" >&2
    printf '%s' "$primary_json" | head -c 300 | sed 's/^/  /' >&2; echo >&2
    exit 4
  fi
  if [[ "$n_primary" == "0" ]]; then
    echo "pd_worklist.sh: no papers matched query '$TAX_ID' (try fewer/other words, or a taxonomy target)" >&2
    exit 0
  fi
  # Thin pool — only reachable now via --search-mode title|auto, since the
  # default pins semantic. Kept because the auto waterfall accepts the title
  # layer at >=3 hits and never reaches the vector layer.
  if [[ "$layer_used" == "title" && "$n_primary" -lt "$LIMIT" ]]; then
    cat >&2 <<EOF
pd_worklist.sh: NOTE: the search stopped at the title layer with $n_primary/$LIMIT rows.
  The semantic layer was never reached, so this pool is probably thinner than the
  corpus actually holds. Re-run without --search-mode (the default pins the
  semantic layer), or take the best hit as a seed and run --paper <id>.
EOF
  fi
  # Semantic-layer miss: the batch is full-length but landed in the wrong
  # cluster. Both tells are in the response, so check them rather than reading
  # row count as health. Measured: on-target top-1 scores 0.62-0.76; a
  # keyword-length query scored 0.52-0.54 with a why[] cluster from an
  # unrelated field.
  if [[ "$layer_used" == "semantic" ]]; then
    top_score=$(printf '%s' "$primary_json" | jq -r '[.items[].score // 0] | max // 0')
    top_why=$(printf '%s' "$primary_json" | jq -r '[.items[].why // []] | flatten
                   | map(select(startswith("in dominant cluster"))) | first // ""')
    if awk -v s="$top_score" 'BEGIN{exit !(s < 0.60)}'; then
      cat >&2 <<EOF
pd_worklist.sh: WARNING: semantic top-1 score is $top_score (<0.60) — this query
  probably did NOT hit the corpus, even though it returned $n_primary rows.
  ${top_why:+Reranked cluster was: $top_why — is that your field?}
  Rewrite the target as a longer, more specific sentence and re-run. Do NOT carry
  a missed pool into stage 1.5; a full-length wrong-cluster pool looks healthy at
  every downstream gate.
EOF
    else
      echo "pd_worklist.sh: semantic top-1 score=$top_score ${top_why:+($top_why)}" >&2
    fi
  fi
else
  papers_q="/papers?${LEVEL}_id=${TAX_ID}&has_extraction=true&limit=${LIMIT}"
  [[ -n "$YEAR_FROM" ]] && papers_q="${papers_q}&year_from=${YEAR_FROM}"

  primary_json=$(pd_api GET "$papers_q") || { echo "pd_worklist.sh: failed to fetch primary paper list" >&2; exit 4; }
  n_primary=$(json_len "$primary_json" "items")
  if [[ -z "$n_primary" ]]; then
    echo "pd_worklist.sh: GET /papers returned no .items array — unexpected response shape. First 300 bytes:" >&2
    printf '%s' "$primary_json" | head -c 300 | sed 's/^/  /' >&2; echo >&2
    exit 4
  fi

  if [[ "$n_primary" == "0" ]]; then
    echo "pd_worklist.sh: no papers matched ${LEVEL}_id=${TAX_ID} (try relaxing --year-from)" >&2
    exit 0
  fi
fi

# ── 5. merge primary + similar-neighbour expansion, deduped, ordered ───
ids_only_file=$(mktemp "${TMPDIR:-/tmp}/pd_worklist_ids.XXXXXX")
ids_tsv_file=$(mktemp "${TMPDIR:-/tmp}/pd_worklist_tsv.XXXXXX")
rows_file=$(mktemp "${TMPDIR:-/tmp}/pd_worklist_rows.XXXXXX")
trap 'rm -f "$ids_only_file" "$ids_tsv_file" "$rows_file" "$_HDR_FILE"' EXIT

# Papers the API refused to hand over. Counted, never swallowed: a worklist
# that is short because of rate limiting must not be mistaken for a small field.
N_SIMILAR_FAILED=0
N_DETAIL_FAILED=0

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
    N_SIMILAR_FAILED=$((N_SIMILAR_FAILED + 1))
    echo "pd_worklist.sh: warning: similar-fetch failed for seed $SEED_ID — worklist will be the seed alone" >&2
  else
    while IFS= read -r sid; do
      [[ -n "$sid" ]] && add_id "$sid" "similar"
    done < <(printf '%s' "$sim_json" | jq -r '.items[].paper.id' 2>/dev/null)
  fi
else
  primary_ids=$(printf '%s' "$primary_json" | jq -r '.items[].id')
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && add_id "$pid" "primary"
  done <<<"$primary_ids"
  n_primary_added=$(wc -l < "$ids_only_file" | tr -d ' ')

  if [[ "$SIMILAR" -gt 0 ]]; then
    n_before_similar=$(wc -l < "$ids_only_file" | tr -d ' ')
    while IFS= read -r pid; do
      [[ -n "$pid" ]] || continue
      sim_json=""
      if ! sim_json=$(pd_api GET "/papers/${pid}/similar?k=${SIMILAR}"); then
        N_SIMILAR_FAILED=$((N_SIMILAR_FAILED + 1))
        echo "pd_worklist.sh: warning: similar-fetch failed for $pid, skipping expansion" >&2
      else
        while IFS= read -r sid; do
          [[ -n "$sid" ]] && add_id "$sid" "similar"
        done < <(printf '%s' "$sim_json" | jq -r '.items[].paper.id' 2>/dev/null)
      fi
    done <<<"$primary_ids"
    if [[ "$(wc -l < "$ids_only_file" | tr -d ' ')" == "$n_before_similar" ]]; then
      cat >&2 <<EOF
pd_worklist.sh: WARNING: --similar $SIMILAR added ZERO new papers.
  On this corpus a paper's nearest neighbour is often its own twin vertex, which
  the pool already holds — at low k the expansion contributes nothing. Re-run with
  a larger --similar (3-4) if you wanted a wider pool. Raising k costs no extra
  requests, only a longer result list per call.
EOF
    fi
  fi
fi

n_merged=$(wc -l < "$ids_only_file" | tr -d ' ')

# ── 6. per-paper detail -> rows, then twin-collapse -> worklist.jsonl ──
# --append seeds the row buffer with the existing pool so a second batch merges
# into the first instead of overwriting it. SKILL.md told readers to re-run into
# the same <slug> for another batch; before 0.4.2 that silently destroyed the
# previous pool. The collapse pass below then dedups across both batches.
: > "$rows_file"
n_appended=0
if [[ "$APPEND" == "1" && -s "$OUT/worklist.jsonl" ]]; then
  cat "$OUT/worklist.jsonl" >> "$rows_file"
  n_appended=$(wc -l < "$OUT/worklist.jsonl" | tr -d ' ')
  echo "pd_worklist.sh: --append: carrying $n_appended existing row(s) into the merge" >&2
fi
n_rows=0
while IFS=$'\t' read -r pid src; do
  [[ -n "$pid" ]] || continue
  detail_json=""
  if ! detail_json=$(pd_api GET "/papers/${pid}"); then
    N_DETAIL_FAILED=$((N_DETAIL_FAILED + 1))
    echo "pd_worklist.sh: warning: detail-fetch failed for $pid, skipping" >&2
  else
    # paperdaily's paper_etl writes several missing string attrs as "" not
    # JSON null (the API has historically returned "" for absent fields);
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
        # The PDF url paperdaily'"'"'s own worker already resolved (v1 0.8.87+).
        # Kept as its own field rather than folded into oa_url: it is a real
        # PDF we reached, whereas oa_url degrades to a doi.org landing page.
        # Absent on older servers -> null, and stage 2 just skips that layer.
        pdf_url: (.resolved_pdf_url | blank_to_null),
        source: $source
      }' >> "$rows_file"
    n_rows=$((n_rows + 1))
  fi
done < "$ids_tsv_file"
n_rows=$((n_rows + n_appended))

# ── twin collapse ────────────────────────────────────────────────────
# The same work reaches the pool under several ids, because they are separate
# vertices in the graph and step 5's id-level dedup cannot see it (identifiers
# only arrive with the detail fetch). Three shapes, all measured on real pools:
#
#   1. `arxiv:NNNN` vs an OpenAlex `W…` whose DOI is `10.48550/arxiv.NNNN`
#   2. `arxiv:NNNN` vs a `W…` with BOTH doi and arxiv_id null whose oa_url is
#      literally `https://arxiv.org/pdf/NNNN`   (0.4.1 missed this one)
#   3. the same work under two genuinely DIFFERENT DOIs — venue DOI vs arXiv
#      DOI vs Underline DOI (0.4.1 missed this one too; e.g. MIRAGE under both
#      10.48448/5ad5-d532 and 10.18653/v1/2025.findings-naacl.157)
#
# Measured before 0.4.2: an 18-row pool held 5 twin pairs = 13 distinct works.
#
# This is not cosmetic. Stage 1.5 would spend two reading slots on one paper,
# and — worse — `claims.jsonl` defines `supported` as "at least two INDEPENDENT
# papers agreeing", so a paper counted twice can promote a weak claim to
# supported. That is false confidence in the one artifact whose job is honesty.
#
# Pass 1 collapses on hard identifiers (no false positives possible). Pass 2
# collapses identical normalised titles, which is what catches shape 3 — but it
# REFUSES to merge when both rows carry an arxiv_id and the two differ, because
# distinct arXiv submissions are distinct works however similar their titles.
# Every collapse is recorded to worklist.twins.jsonl and printed, so a wrong
# merge stays visible and nothing disappears without an audit trail.
#
# Surviving row: the one carrying arxiv_id (it gives Stage 2 the L0 fast path),
# with the STRONGEST source of the pair (seed > primary > similar) so a primary
# row arriving after its similar-expanded twin is not demoted.
jq -s '
  def norm_arxiv: ascii_downcase | sub("^arxiv:"; "") | sub("v[0-9]+$"; "");
  def arxiv_from_url:
    . as $u
    | if ($u | type) != "string" then null
      elif ($u | test("arxiv\\.org/(pdf|abs)/"; "i"))
      then ($u | sub("^.*arxiv\\.org/(pdf|abs)/"; ""; "i")
               | sub("\\.pdf$"; ""; "i") | sub("[?#].*$"; "") | norm_arxiv)
      else null end;
  def hardkey:
    ((.arxiv_id // "") | tostring) as $a
    | ((.doi // "") | tostring | ascii_downcase) as $d
    | ((.oa_url // "") | arxiv_from_url) as $ua
    | if ($a | length) > 0 then "arxiv:" + ($a | norm_arxiv)
      elif ($d | test("^10\\.48550/arxiv\\.")) then
        "arxiv:" + (($d | sub("^10\\.48550/arxiv\\."; "")) | norm_arxiv)
      elif ($ua != null and ($ua | length) > 0) then "arxiv:" + $ua
      elif ($d | length) > 0 then "doi:" + $d
      else "id:" + (.id | ascii_downcase)
      end;
  def titlekey:
    ((.title // "") | tostring | ascii_downcase
     | gsub("[^a-z0-9]+"; " ") | gsub("^ +| +$"; ""));
  def better($a; $b): if ($b.arxiv_id != null and $a.arxiv_id == null) then $b else $a end;
  def srank: if . == "seed" then 0 elif . == "primary" then 1 else 2 end;
  def strongest($a; $b):
    if (($b.source | srank) < ($a.source | srank)) then $b.source else $a.source end;
  def collapse($kind):
    reduce range(0; (.rows | length)) as $i ({order: [], best: {}, twins: .twins, src: .rows};
        (.src[$i]) as $r
        | (if $kind == "hard" then ($r | hardkey)
           else (($r | titlekey) as $t
                 | if ($t | length) == 0 then "__untitled__" + ($i | tostring) else $t end)
           end) as $k
        | (if $kind == "title"
             and (.best | has($k))
             and (.best[$k].arxiv_id != null) and ($r.arxiv_id != null)
             and ((.best[$k].arxiv_id | norm_arxiv) != ($r.arxiv_id | norm_arxiv))
           then false else (.best | has($k)) end) as $merge
        | if $merge
          then (better(.best[$k]; $r)) as $win
               | .twins += [{kind: $kind, kept: $win.id,
                             dropped: (if $win.id == $r.id then .best[$k].id else $r.id end),
                             title: ($r.title // .best[$k].title)}]
               | .best[$k] = ($win + {source: strongest(.best[$k]; $r)})
          else (.order += [(if (.best | has($k)) then $k + "#" + ($i | tostring) else $k end)])
               | .best[(if (.best | has($k)) then $k + "#" + ($i | tostring) else $k end)] = $r
          end)
    | . as $a
    | {rows: ($a.order | map($a.best[.])), twins: $a.twins};
  {rows: ., twins: []} | collapse("hard") | collapse("title")
' "$rows_file" > "$rows_file.collapsed"

jq -c '.rows[]'  "$rows_file.collapsed" > "$OUT/worklist.jsonl"
jq -c '.twins[]' "$rows_file.collapsed" > "$OUT/worklist.twins.jsonl"

n_written=$(wc -l < "$OUT/worklist.jsonl" | tr -d ' ')
N_TWINS_MERGED=$(wc -l < "$OUT/worklist.twins.jsonl" | tr -d ' ')
if [[ "$N_TWINS_MERGED" -gt 0 ]]; then
  echo "pd_worklist.sh: collapsed $N_TWINS_MERGED twin row(s) — audit trail in worklist.twins.jsonl:" >&2
  jq -r '"  [\(.kind)] kept \(.kept), dropped \(.dropped) — \(.title[0:60])"' \
    "$OUT/worklist.twins.jsonl" >&2
fi
rm -f "$rows_file.collapsed"

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
echo "primary papers: $n_primary_added   similar-expanded: $((n_merged - n_primary_added))   merged unique ids: $n_merged"
if [[ "${N_TWINS_MERGED:-0}" -gt 0 ]]; then
  echo "twin rows collapsed: $N_TWINS_MERGED (same work under several ids — see worklist.twins.jsonl)"
fi
# Deliberately NOT phrased as "distinct works". The collapse catches hard
# identifiers and identical titles; a cross-version duplicate whose title was
# rewritten still slips through. Claiming uniqueness we cannot deliver is worse
# than claiming nothing — Stage 1.5 is told to eyeball titles because of this.
echo "worklist rows written: $n_written  (post-collapse; known duplicates removed, not a uniqueness guarantee)"
echo "worklist: $OUT/worklist.jsonl"
if [[ "$DO_SYNTH" == "1" && -f "$OUT/overview.md" ]]; then
  echo "overview: $OUT/overview.md"
fi

# Incompleteness is reported, never implied. A caller (human or agent) reading
# only "worklist rows written: 12" cannot tell a 12-paper field from a
# 50-paper field that lost 38 rows to the quota — so say which it was.
if [[ "$N_DETAIL_FAILED" -gt 0 || "$N_SIMILAR_FAILED" -gt 0 ]]; then
  echo
  echo "!! WORKLIST INCOMPLETE — $N_DETAIL_FAILED paper(s) dropped (detail fetch failed),"
  echo "   $N_SIMILAR_FAILED similar-expansion(s) skipped; $N_RETRY_WAITS rate-limit wait(s) along the way."
  echo "   Do NOT treat this as the size of the field. Either re-run (already-seen"
  echo "   ids are cheap to re-resolve) or raise --throttle above ${THROTTLE}s if the"
  echo "   failures were 429s. Check the warnings above for the actual HTTP codes."
elif [[ "$N_RETRY_WAITS" -gt 0 ]]; then
  echo "(paced through $N_RETRY_WAITS rate-limit wait(s); no papers lost)"
fi
