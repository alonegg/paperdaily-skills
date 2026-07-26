#!/usr/bin/env bash
# pd_inbox.sh — Stage 0 (optional) of the paperdaily deep-research skill:
# the agent-task inbox. Lists the tasks the user queued for their agent on
# the paperdaily web UI (e.g. detail page "转给我的 agent 深读") and claims
# one once the user confirms they want it worked on.
#
# Usage:
#   pd_inbox.sh                  # table of pending tasks (id/kind/papers/created/note)
#   pd_inbox.sh --json           # raw {items:[...]} JSON — payload passed through
#                                # verbatim (read paper_ids / seed_paper_ids there)
#   pd_inbox.sh --claim <id>     # claim one task (pending → claimed)
#
# Endpoints (AGENT_PROTOCOL §5, A1+A2 / 0.8.x):
#   GET  /me/agent-tasks?status=pending     scope read:reading
#        → {items:[{id, kind, payload, status, created_at}]}
#        kind is a passthrough field; payload shape depends on it:
#          deep_read        {paper_ids[], note?}                (A1)
#          answer_question  {question_id, seed_paper_ids[]}     (A2)
#   POST /me/agent-tasks/{id}/claim         scope write:reading
#        → 200 claimed · 409 already claimed/closed (benign — pick another)
#
# Scope soft-degrade: the inbox is an *optional* stage. A key without
# read:reading (403 on list) or write:reading (403 on claim) gets a short
# how-to-fix note and exit 0 — not an error. A 404 on the list endpoint
# means the server predates A1: also a note + exit 0.
#
# Auth: PD_BASE + PD_KEY from the environment; only when unset, sourced
# from ~/.paperdaily-cli/env (same convention as pd_worklist.sh).
#
# Self-contained: only bash + curl + jq. bash 3.2 (macOS default) compatible.
# Exit codes: 0 ok / benign (missing scope, empty inbox, endpoint absent,
# 409 on claim) · 1 missing deps/credentials · 2 usage · 3 HTTP/network failure.

set -euo pipefail

command -v curl >/dev/null 2>&1 || { echo "pd_inbox.sh: curl not found" >&2; exit 1; }
command -v jq   >/dev/null 2>&1 || { echo "pd_inbox.sh: jq not found (brew install jq / apt install jq)" >&2; exit 1; }

# ── 0. auth (only source the env file when not already set) ───────────
if [[ -z "${PD_KEY:-}" || -z "${PD_BASE:-}" ]]; then
  if [[ -r "$HOME/.paperdaily-cli/env" ]]; then
    # shellcheck disable=SC1090
    source "$HOME/.paperdaily-cli/env"
  fi
fi
if [[ -z "${PD_KEY:-}" || -z "${PD_BASE:-}" ]]; then
  cat >&2 <<'EOF'
pd_inbox.sh: missing PD_BASE / PD_KEY.

Create ~/.paperdaily-cli/env with:
  export PD_BASE="https://www.paperdaily.org/api/v1"
  export PD_KEY="pd_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

No key yet? Issue one from the paperdaily web UI:
  /account → API Keys → Issue key   (inbox list: read:reading; claim: write:reading)
EOF
  exit 1
fi

# ── 1. arg parsing ───────────────────────────────────────────────────
MODE="list"
CLAIM_ID=""
AS_JSON=0

print_help() { sed -n '2,33p' "$0"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --claim)
      [[ $# -ge 2 && -n "${2:-}" ]] || { echo "pd_inbox.sh: --claim needs a task id" >&2; exit 2; }
      MODE="claim"; CLAIM_ID=$2; shift 2 ;;
    --json)     AS_JSON=1; shift ;;
    --help|-h)  print_help; exit 0 ;;
    *)          echo "pd_inbox.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
done

# ── 2. tiny HTTP client: prints the status code, body → $BODY_FILE ────
# (the status must travel outside a command substitution, so the body
# goes to a temp file instead of stdout; retries once on 429)
BODY_FILE=$(mktemp "${TMPDIR:-/tmp}/pd_inbox_body.XXXXXX")
trap 'rm -f "$BODY_FILE"' EXIT

pd_api() {
  local method="$1" path="$2"
  local url="${PD_BASE}${path}"
  local code attempt
  for attempt in 1 2; do
    code=$(curl -sS -o "$BODY_FILE" -w '%{http_code}' \
      -H "Authorization: Bearer $PD_KEY" -X "$method" "$url" 2>/dev/null) || code="000"
    if [[ "$code" == "429" && "$attempt" == "1" ]]; then
      echo "pd_inbox.sh: 429 on $method $path — backing off 3s, retrying once" >&2
      sleep 3
      continue
    fi
    break
  done
  printf '%s' "$code"
}

dump_body() {
  # first 400 bytes of the response body, indented, to stderr
  head -c 400 "$BODY_FILE" | sed -e 's/^/  /' >&2 || true
  echo >&2
}

scope_hint() {
  # 403 = key lacks the needed scope. Optional stage → guidance, exit 0.
  local scope="$1"
  cat >&2 <<EOF
pd_inbox.sh: your key lacks the ${scope} scope — inbox skipped (this is not an error).
To use the agent-task inbox, issue a key with it checked:
  paperdaily web UI → /account → API Keys → Issue key → check ${scope}
then update PD_KEY in ~/.paperdaily-cli/env and re-run. Proceeding without
the inbox is fine: go straight to Stage 1 (pd_worklist.sh).
EOF
}

# ── 3. claim mode ────────────────────────────────────────────────────
if [[ "$MODE" == "claim" ]]; then
  code=$(pd_api POST "/me/agent-tasks/${CLAIM_ID}/claim")
  case "$code" in
    200)
      echo "pd_inbox.sh: claimed task ${CLAIM_ID}"
      echo "next: seed Stage 1 with the task's first paper id (payload.paper_ids /"
      echo "      seed_paper_ids) — pd_worklist.sh --paper <paper_id>"
      exit 0 ;;
    409)
      echo "pd_inbox.sh: task ${CLAIM_ID} is already claimed or closed (409) — benign, do not retry; pick another task or proceed without one" >&2
      exit 0 ;;
    403)
      scope_hint "write:reading"; exit 0 ;;
    401)
      echo "pd_inbox.sh: HTTP 401 — key missing/invalid/revoked; check PD_KEY in ~/.paperdaily-cli/env" >&2
      exit 3 ;;
    404)
      echo "pd_inbox.sh: HTTP 404 — task '${CLAIM_ID}' not found, or this server has no agent-tasks endpoint yet (pre-A1). Response:" >&2
      dump_body
      exit 3 ;;
    000)
      echo "pd_inbox.sh: network failure reaching ${PD_BASE} — check connectivity / PD_BASE" >&2
      exit 3 ;;
    *)
      echo "pd_inbox.sh: POST /me/agent-tasks/${CLAIM_ID}/claim → HTTP ${code}:" >&2
      dump_body
      exit 3 ;;
  esac
fi

# ── 4. list mode ─────────────────────────────────────────────────────
code=$(pd_api GET "/me/agent-tasks?status=pending")
case "$code" in
  200) : ;;
  403)
    scope_hint "read:reading"; exit 0 ;;
  401)
    echo "pd_inbox.sh: HTTP 401 — key missing/invalid/revoked; check PD_KEY in ~/.paperdaily-cli/env" >&2
    exit 3 ;;
  404)
    echo "pd_inbox.sh: this server has no /me/agent-tasks endpoint yet (pre-A1 deployment) — inbox unavailable; proceed straight to Stage 1 (pd_worklist.sh)." >&2
    exit 0 ;;
  000)
    echo "pd_inbox.sh: network failure reaching ${PD_BASE} — check connectivity / PD_BASE" >&2
    exit 3 ;;
  *)
    echo "pd_inbox.sh: GET /me/agent-tasks?status=pending → HTTP ${code}:" >&2
    dump_body
    exit 3 ;;
esac

# guard: a 200 that is not the {items:[...]} contract (e.g. an SPA fallback
# serving HTML on an unknown path) must fail loudly, not be parsed as tasks.
if ! jq -e '.items | type == "array"' "$BODY_FILE" >/dev/null 2>&1; then
  echo "pd_inbox.sh: unexpected response shape from GET /me/agent-tasks (no .items array) — server likely predates A1. Response starts with:" >&2
  dump_body
  exit 3
fi

if [[ "$AS_JSON" == "1" ]]; then
  cat "$BODY_FILE"
  echo
  exit 0
fi

n_tasks=$(jq '.items | length' "$BODY_FILE")
if [[ "$n_tasks" == "0" ]]; then
  echo "pd_inbox.sh: inbox is empty — no pending tasks. Proceed straight to Stage 1."
  exit 0
fi

echo "== agent-task inbox: ${n_tasks} pending =="
# KIND is a passthrough field — deep_read and answer_question both fit the
# %-15s min-width column (no truncation). PAPERS counts paper_ids (deep_read)
# or seed_paper_ids (answer_question). NOTE shows payload.note; an
# answer_question task has no note, so its question_id is shown instead
# (fetch the question text via GET /me/questions/<question_id>,
# scope read:reading — AGENT_PROTOCOL §5).
printf '%-16s  %-15s  %6s  %-19s  %s\n' "ID" "KIND" "PAPERS" "CREATED_AT" "NOTE"
jq -r '.items[] | [
    .id,
    .kind,
    ((.payload.paper_ids // .payload.seed_paper_ids // []) | length | tostring),
    ((.created_at // "?") | .[0:19]),
    ((.payload.note
        // (if .payload.question_id != null then "question:\(.payload.question_id)" else null end)
        // "-")
      | gsub("[\\n\\t\\r]"; " ") | .[0:60])
  ] | @tsv' "$BODY_FILE" \
| while IFS=$'\t' read -r tid kind np created note; do
    printf '%-16s  %-15s  %6s  %-19s  %s\n' "$tid" "$kind" "$np" "$created" "$note"
  done

echo
echo "next: pick one WITH the user (claim only after they confirm), then:"
echo "  pd_inbox.sh --json                 # read the chosen task's payload (paper_ids / seed_paper_ids)"
echo "  pd_inbox.sh --claim <id>           # claim it"
echo "  pd_worklist.sh --paper <paper_id>  # seed Stage 1 with the first paper_id"
