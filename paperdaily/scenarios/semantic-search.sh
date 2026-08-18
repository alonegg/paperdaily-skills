#!/usr/bin/env bash
# Semantic retrieval over the paperdaily corpus — for fuzzy research
# directions ("模糊领域") and for "find papers like these ones".
#
# Usage:
#   # concept mode — a research direction that is NOT a taxonomy node
#   semantic-search.sh "retrieval augmented generation reduces hallucination"
#   semantic-search.sh "LLM 个性化学习" --limit 20 --scope auto
#   semantic-search.sh "staggered difference-in-differences" --scope 2002 --year-from 2023
#
#   # fan-out — you supply the paraphrases, the script fuses them (RRF)
#   semantic-search.sh "AI tutors improve student outcomes" \
#       --expand "LLM 个性化学习 自适应教学" \
#       --expand "generative AI in classroom teaching practice"
#
#   # seed mode — papers similar to one or more given papers
#   semantic-search.sh --paper W7165154520
#   semantic-search.sh --paper arxiv:2201.11903 --paper W7133224126 --limit 30
#
#   --json   machine-readable output instead of the markdown table
#
# Why this script exists (all numbers measured on prod 2026-08-18):
#   * `mode=auto` runs the trgm TITLE layer first and accepts it at ≥3 hits.
#     A conceptual query therefore often gets 3 lexical rows and never
#     reaches the vector layer — "mixture of experts routing": auto = 19.6s
#     / 3 rows, pinned semantic = 5.1s / 20 rows. This script always pins
#     `mode=semantic`.
#   * One query vector is one point. Four paraphrases of the SAME question
#     returned 80 distinct papers with ZERO overlap — fan-out is not
#     redundant work, it is the only way to cover a direction.
#   * `/similar` top-k spans a cosine radius of only ~0.02 while two records
#     of the *same* paper sit 0.04 apart — a single seed's neighbourhood is
#     a narrow cone. Use several seeds, and union with the text layer.
#
# Scope: read:paper. Pure read, 0 credits. No /ask, no writes.

set -uo pipefail
source "$(dirname "$0")/../_lib.sh"

LIMIT=20
SCOPE=""            # "" | auto | <subfield_id> | field:<field_id>
YEAR_FROM=""
YEAR_TO=""
JSON=0
THROTTLE=1.05       # read:paper free tier is 60/min (fixed window)
QUERY=""
declare -a EXPAND=()
declare -a SEEDS=()
declare -a SEED_IDS=()
declare -a SEED_TITLES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit)      LIMIT=$2; shift 2 ;;
    --scope)      SCOPE=$2; shift 2 ;;
    --year-from)  YEAR_FROM=$2; shift 2 ;;
    --year-to)    YEAR_TO=$2; shift 2 ;;
    --expand)     EXPAND+=("$2"); shift 2 ;;
    --paper)      SEEDS+=("$2"); shift 2 ;;
    --throttle)   THROTTLE=$2; shift 2 ;;
    --json)       JSON=1; shift ;;
    --help|-h)    sed -n '2,40p' "$0"; exit 0 ;;
    -*)           echo "unknown flag: $1" >&2; exit 2 ;;
    *)            QUERY="${QUERY}${QUERY:+ }$1"; shift ;;
  esac
done

if [[ -z "$QUERY" && ${#SEEDS[@]} -eq 0 ]]; then
  echo "usage: semantic-search.sh \"<research direction>\" [--expand ...] | --paper <id> [--paper <id>]" >&2
  exit 2
fi

urlenc() { jq -rn --arg s "$1" '$s|@uri'; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
NCALL=0

# GET with 429 backoff (honours Retry-After, ≤3 tries). Prints body on stdout,
# empty string + non-zero on give-up.
pd_fetch() {
  local path=$1 try=0 code body
  while :; do
    [[ $NCALL -gt 0 ]] && sleep "$THROTTLE"
    NCALL=$((NCALL + 1))
    body=$(curl -sS -w $'\n%{http_code}' -H "Authorization: Bearer $PD_KEY" "$PD_BASE$path" 2>/dev/null)
    code=${body##*$'\n'}; body=${body%$'\n'*}
    if [[ "$code" == "200" ]]; then printf '%s' "$body"; return 0; fi
    if [[ "$code" == "429" && $try -lt 3 ]]; then
      try=$((try + 1)); echo "semantic-search: 429 on $path — backing off ${try}0s" >&2
      sleep "${try}0"; continue
    fi
    echo "semantic-search: GET $path -> HTTP $code" >&2
    printf '%s' "$body" | head -c 200 | sed 's/^/  /' >&2; echo >&2
    return 1
  done
}

# One pinned-semantic search. $1=query $2=scope-suffix -> writes $TMP/r<N>.json
sem_search() {
  local q=$1 suffix=${2:-} out=$3
  local path="/papers/search?q=$(urlenc "$q")&mode=semantic&limit=${LIMIT}${suffix}"
  local json; json=$(pd_fetch "$path") || return 1
  printf '%s' "$json" | jq --arg q "$q" '
    {query:$q, layer:(.layer_used//"?"), took_ms:(.took_ms//0),
     items:[(.items//[])[] | {id,title,publication_date,subfield_id,field_id,
                              score:(.score//0), src:"sem", why:(.why//[])}]}' > "$out"
}

scope_suffix() {
  local s=$1 out=""
  case "$s" in
    "")            : ;;
    field:*)       out="&field_id=${s#field:}" ;;
    *)             out="&subfield_id=${s}" ;;
  esac
  [[ -n "$YEAR_FROM" ]] && out="${out}&year_from=${YEAR_FROM}"
  [[ -n "$YEAR_TO"   ]] && out="${out}&year_to=${YEAR_TO}"
  printf '%s' "$out"
}

# ── seed mode ────────────────────────────────────────────────────────
if [[ ${#SEEDS[@]} -gt 0 ]]; then
  [[ $LIMIT -gt 38 ]] && echo "semantic-search: NOTE: k>38 is capped by the HNSW ef_search default — asking for $LIMIT will return ~38. That is the index, not the corpus." >&2
  i=0
  for seed in "${SEEDS[@]}"; do
    i=$((i + 1))
    resolved=$(pd_fetch "/papers/resolve?id=$(urlenc "$seed")") || {
      echo "semantic-search: seed '$seed' not resolvable — skipping (use a q= search to find it first)" >&2; continue; }
    sid=$(printf '%s' "$resolved" | jq -r '.id // empty')
    stitle=$(printf '%s' "$resolved" | jq -r '.title // ""')
    [[ -n "$sid" ]] || { echo "semantic-search: seed '$seed' resolved to no id — skipping" >&2; continue; }
    echo "semantic-search: seed $i = $sid  ${stitle:0:60}" >&2

    SEED_IDS+=("$sid")
    SEED_TITLES+=("$stitle")
    # (a) vector neighbours of the seed
    if sim=$(pd_fetch "/papers/${sid}/similar?k=${LIMIT}"); then
      printf '%s' "$sim" | jq --arg s "$sid" '
        {query:("similar:"+$s), layer:"similar", took_ms:0,
         items:[(.items//[])[] | {id:.paper.id, title:.paper.title,
                                  publication_date:.paper.publication_date,
                                  subfield_id:null, field_id:null,
                                  score:(1 - (.distance//1)), src:"sim", why:[]}]}' > "$TMP/seed_sim_$i.json"
    fi
    # (b) the seed's TITLE through the text-vector layer — measured 1/20
    #     overlap with (a), i.e. these two are complementary, not redundant.
    if [[ -n "$stitle" ]]; then
      sem_search "$stitle" "$(scope_suffix "$SCOPE")" "$TMP/seed_sem_$i.json" || true
    fi
  done
  shopt -s nullglob
  files=("$TMP"/seed_*.json)
  [[ ${#files[@]} -gt 0 ]] || { echo "semantic-search: no seed produced results" >&2; exit 1; }
  MODE_LABEL="seed"
  SEED_CSV=$(printf '%s,' "${SEEDS[@]}"); SEED_CSV=${SEED_CSV%,}
else
  # ── concept mode ──────────────────────────────────────────────────
  nwords=$(printf '%s' "$QUERY" | wc -w | tr -d ' ')
  if [[ $nwords -lt 4 ]]; then
    cat >&2 <<EOF
semantic-search: WARNING: "$QUERY" is $nwords word(s). Short keyword queries
  poison this engine — measured: "chain-of-thought prompting" returned
  Educational-Leadership editorials at score 0.52, while the full sentence
  "chain-of-thought prompting elicits reasoning in large language models"
  put the real paper first at 0.759. Describe the direction in a sentence.
EOF
  fi

  sfx=$(scope_suffix "$SCOPE")
  [[ "$SCOPE" == "auto" ]] && sfx=$(scope_suffix "")   # probe unscoped first
  sem_search "$QUERY" "$sfx" "$TMP/q0.json" || exit 1

  if [[ "$SCOPE" == "auto" ]]; then
    # The subfield distribution of an unscoped probe IS the scope discovery
    # mechanism — measured clean: "staggered DiD" -> 30/30 subfield 2613,
    # "RAG hallucination" -> 30/30 subfield 1702.
    dom=$(jq -r '[.items[].subfield_id | select(.!=null)] | group_by(.)
                 | map({k:.[0], n:length}) | sort_by(-.n) | .[0].k // empty' "$TMP/q0.json")
    if [[ -n "$dom" ]]; then
      echo "semantic-search: probe landed in subfield $dom — re-running scoped" >&2
      sem_search "$QUERY" "$(scope_suffix "$dom")" "$TMP/q0s.json" || true
      SCOPE="$dom"
    else
      echo "semantic-search: probe found no dominant subfield — staying unscoped" >&2
    fi
  fi

  j=0
  for v in "${EXPAND[@]:-}"; do
    [[ -n "$v" ]] || continue
    j=$((j + 1))
    sem_search "$v" "$(scope_suffix "$([[ "$SCOPE" == auto ]] && echo "" || echo "$SCOPE")")" "$TMP/q$j.json" || true
  done
  shopt -s nullglob
  files=("$TMP"/q*.json)
  MODE_LABEL="concept"
  SEED_CSV=""
fi

# ── fuse: RRF across every result list + collapse title twins ─────────
# RRF (k=60) rather than raw score, for two independent reasons:
#   * per-query scores are not comparable (each list gets its own
#     dominant-topic rerank), only ranks are;
#   * in seed mode the two channels are on different scales entirely —
#     `sim` is 1−cosine-distance (0.97+ is normal), `sem` is the reranked
#     composite (0.6+ is good). Never sort those two in one numeric column;
#     they are reported separately below.
SEED_ID_JSON=$(printf '%s\n' "${SEED_IDS[@]:-}" | jq -R . | jq -sc 'map(select(length>0))')
# Excluding the seed by id is not enough: the same paper is in the graph
# under several ids (arXiv record + OpenAlex W- record), so the seed comes
# back as its own twin. Exclude by normalised title too.
SEED_TITLE_JSON=$(printf '%s\n' "${SEED_TITLES[@]:-}" | jq -R . \
  | jq -sc 'map(select(length>0) | ascii_downcase | gsub("[^a-z0-9\\u4e00-\\u9fff]+";""))')
FUSED=$(jq -s --arg mode "$MODE_LABEL" --argjson seeds "$SEED_ID_JSON" \
           --argjson seedtitles "$SEED_TITLE_JSON" '
  def norm: ascii_downcase | gsub("[^a-z0-9\\u4e00-\\u9fff]+";"");
  ([.[] | .query] ) as $queries
  | [ .[] | .query as $q | (.items | to_entries[])
      | {id:.value.id, title:.value.title, pub:.value.publication_date,
         subfield_id:.value.subfield_id, score:.value.score, src:.value.src,
         why:.value.why, q:$q, rank:(.key+1), rrf:(1.0/(60+.key+1))} ]
  | map(select(.id as $i | ($seeds | index($i)) | not))   # never recommend the seed back
  | group_by(.title | norm)                     # twin collapse: same title, several ids
  | map({ id:        (.[0].id),
          all_ids:   ([.[].id] | unique),
          title:     (.[0].title),
          pub:       (.[0].pub),
          subfield_id:([.[] | .subfield_id | select(.!=null)] | first),
          sim_score: ([.[] | select(.src=="sim") | .score] | max),
          sem_score: ([.[] | select(.src=="sem") | .score] | max),
          channels:  ([.[].src] | unique | join("+")),
          hits:      ([.[].q] | unique | length),
          from:      ([.[].q] | unique),
          why:       ([.[] | .why[]] | unique),
          rrf:       ([.[].rrf] | add) })
  | map(select((.title | norm) as $t | ($seedtitles | index($t)) | not))
  | sort_by(-.hits, -.rrf)
  | {mode:$mode, n_queries:($queries|unique|length), queries:($queries|unique),
     n_results:length, items:.}' "${files[@]}")

if [[ $JSON -eq 1 ]]; then
  printf '%s\n' "$FUSED"
  exit 0
fi

# ── markdown report ──────────────────────────────────────────────────
nq=$(printf '%s' "$FUSED" | jq -r '.n_queries')
nr=$(printf '%s' "$FUSED" | jq -r '.n_results')
top=$(printf '%s' "$FUSED" | jq -r '[.items[].sem_score | select(.!=null)] | max // 0')

if [[ -n "$SEED_CSV" ]]; then
  echo "## Papers similar to \`$SEED_CSV\`"
else
  echo "## Semantic search — $QUERY"
fi
echo
echo "- 检索层: \`mode=semantic\` (pinned) · 查询数 $nq · 去重后 $nr 篇"
[[ -n "$SCOPE" && "$SCOPE" != "auto" ]] && echo "- scope: \`$SCOPE\`${YEAR_FROM:+ · year_from=$YEAR_FROM}"
echo

# 0.56 而不是 0.60：实测 miss 落在 <=0.542、hit 从 0.5936 起，0.60 卡在 hit
# 区间里会误伤（`mixture of experts routing in sparse transformers` 0.5936 结果
# 全对却被判 miss）。且 `graph`（1 词）拿 0.6102 —— **分数高不代表查询够具体**，
# 那一类只能靠读 cluster 判，没有阈值抓得到。
awk_warn=$(printf '%s' "$top" | awk '{print ($1 < 0.56) ? "yes" : "no"}')
if [[ "$awk_warn" == "yes" && -z "$SEED_CSV" ]]; then
  cat <<EOF
> ⚠️ 最高分只有 $top。这个语料上 0.56 以下基本都是**查询没打中**（实测 miss
> 0.52-0.54、hit ≥0.59），而不是语料里没有。把查询写成一整句更具体的描述再来一次。

EOF
fi
if [[ -z "$SEED_CSV" ]]; then
  cat <<EOF
> **判是否打中，先看下表 \`why\` 列里的 dominant cluster 是不是你要的领域**——
> 那是强信号，分数只是弱提示（\`graph\` 这种过泛的查询照样能拿 0.61）。

EOF
fi

echo "| # | 命中通道数 | 通道 | sem 分 | sim 近度 | 论文 | subfield | why |"
echo "|---|---|---|---|---|---|---|---|"
printf '%s' "$FUSED" | jq -r '
  def r3: if . == null then "-" else (.*1000|round/1000|tostring) end;
  .items | to_entries[] | select(.key < 25) |
  "| \(.key+1) | \(.value.hits) | \(.value.channels) | \(.value.sem_score|r3) | \(.value.sim_score|r3) | \(.value.title[0:70]) `\(.value.id)` | \(.value.subfield_id // "-") | \(.value.why | join("; ") | .[0:52]) |"'

echo
echo "### 命中分布"
printf '%s' "$FUSED" | jq -r '
  [.items[] | .subfield_id | select(.!=null)] | group_by(.) |
  map("- subfield `\(.[0])`: \(length) 篇") | .[]' 2>/dev/null || true
if [[ $nq -gt 1 ]]; then
  echo
  echo "### 跨通道共识（被 ≥2 个查询/通道命中，优先读这些）"
  printf '%s' "$FUSED" | jq -r '
    [.items[] | select(.hits >= 2)] as $c |
    if ($c|length) == 0 then
      "- 无。**同一问题的多个改写零重叠是这个语料的常态**（实测 4 个改写 × 20 篇 = 80 篇全不重复），说明各查询覆盖的是不同侧面，把它们并起来读，不要指望共识排序。真正会出共识的是 seed 模式的 sim+sem 两条通道。"
    else ($c[] | "- \(.hits)× \(.title[0:70]) `\(.id)`") end'
fi
