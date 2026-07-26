#!/usr/bin/env bash
# Scenario 3: manage watchlists — user-defined topic-tracking jobs that
# scan weekly. v0.1 author-dogfood only; v0.2 will GA.
#
# Sub-commands:
#   watchlist.sh list
#   watchlist.sh show <id>
#   watchlist.sh create  --name N --terms "a,b" [--arxiv "cs.LG,cs.CL"] [--anchors "W1,W2"]
#   watchlist.sh delete  <id>
#   watchlist.sh run     <id>     (manual scan — supported delivery is the weekly cron)
#   watchlist.sh entries <id> [--threat high|med|low] [--since YYYY-MM-DD]
#
# Scope: write:profile (the API holds /me/watchlists under that single
# permission class — same as /me/topics, /me/feedback).

set -euo pipefail
source "$(dirname "$0")/../_lib.sh"

cmd=${1:-}
[[ -n "$cmd" ]] || { sed -n '2,15p' "$0"; exit 2; }
shift

case "$cmd" in
  list)
    pd_get /me/watchlists '[.[] | {id, name, terms: .query_terms, last: .last_scan_date, counts: .last_scan_counts, active: .is_active}]'
    ;;

  show)
    wl_id=${1:?"watchlist id required"}
    pd_get "/me/watchlists/$wl_id"
    ;;

  create)
    NAME=""
    TERMS=""
    ARXIV=""
    ANCHORS=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --name)    NAME=$2; shift 2 ;;
        --terms)   TERMS=$2; shift 2 ;;
        --arxiv)   ARXIV=$2; shift 2 ;;
        --anchors) ANCHORS=$2; shift 2 ;;
        *) echo "unknown flag: $1" >&2; exit 2 ;;
      esac
    done
    [[ -n "$NAME" ]] || { echo "--name required" >&2; exit 2; }
    body=$(python3 -c "
import json,sys
name, terms, arxiv, anchors = sys.argv[1:5]
print(json.dumps({
    'name': name,
    'query_terms': [t.strip() for t in terms.split(',') if t.strip()],
    'arxiv_categories': [a.strip() for a in arxiv.split(',') if a.strip()],
    'anchor_paper_ids': [a.strip() for a in anchors.split(',') if a.strip()],
}))" "$NAME" "$TERMS" "$ARXIV" "$ANCHORS")
    pd_post /me/watchlists "$body"
    ;;

  delete)
    wl_id=${1:?"watchlist id required"}
    pd_delete "/me/watchlists/$wl_id"
    ;;

  run)
    wl_id=${1:?"watchlist id required"}
    # Synchronous — may take 10-60s depending on retrieval breadth.
    pd_post "/me/watchlists/$wl_id/run-now" '{}'
    ;;

  entries)
    wl_id=${1:?"watchlist id required"}
    shift
    qs=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --threat) qs+="threat_level=$2&"; shift 2 ;;
        --since)  qs+="since=$2&"; shift 2 ;;
        --limit)  qs+="limit=$2&"; shift 2 ;;
        *) echo "unknown flag: $1" >&2; exit 2 ;;
      esac
    done
    pd_get "/me/watchlists/$wl_id/entries?${qs}limit=20" \
      '{n: (.items|length), cursor: .next_cursor, by_threat: (.items|group_by(.threat_level)|map({(.[0].threat_level): length})|add), items: [.items[] | {threat: .threat_level, paper_id, title, rationale}]}'
    ;;

  *)
    echo "unknown command: $cmd" >&2
    sed -n '2,15p' "$0"
    exit 2
    ;;
esac
