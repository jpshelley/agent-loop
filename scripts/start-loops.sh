#!/usr/bin/env bash
set -euo pipefail

# A portable tmux launcher for one operator's local Agent Loop installation.
# Configure its tracker and chosen coding harness in ~/.agent-loop/env.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_ENV_FILE="$HOME/.agent-loop/env"
ENV_FILE="${AGENT_LOOP_ENV_FILE:-$DEFAULT_ENV_FILE}"
USER_NAME="${AGENT_LOOP_USER:-${USER:-loop}}"
AGENT_LOOP_BIN="${AGENT_LOOP_BIN:-$REPO_ROOT/.venv/bin/agent-loop}"
FAILURES=0

if [[ ! -r "$ENV_FILE" ]]; then
  echo "Cannot read $ENV_FILE; create it with your tracker credentials and Agent Loop settings." >&2
  exit 1
fi

command -v tmux >/dev/null || {
  echo "tmux is required to start the loop fleet." >&2
  exit 1
}

[[ -x "$AGENT_LOOP_BIN" ]] || {
  echo "agent-loop not found at $AGENT_LOOP_BIN (install it or set AGENT_LOOP_BIN)." >&2
  exit 1
}

start_session() {
  local name="$1"
  local command="$2"

  tmux kill-session -t "$name" 2>/dev/null || true
  if tmux new-session -d -s "$name" -c "$REPO_ROOT" "$command"; then
    echo "started $name"
  else
    echo "failed to start $name" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

loop_command() {
  local loop="$1"
  local interval="$2"
  printf 'set -a; . %q; set +a; exec %q %q --interval %q --user %q' \
    "$ENV_FILE" "$AGENT_LOOP_BIN" "$loop" "$interval" "$USER_NAME"
}

start_session dispatch "$(loop_command dispatch "${AGENT_LOOP_DISPATCH_INTERVAL:-600}")"
start_session develop "$(loop_command develop "${AGENT_LOOP_DEVELOP_INTERVAL:-120}")"
start_session debug "$(loop_command debug "${AGENT_LOOP_DEBUG_INTERVAL:-300}")"
start_session deploy "$(loop_command deploy "${AGENT_LOOP_DEPLOY_INTERVAL:-120}")"
start_session declutter "$(loop_command declutter "${AGENT_LOOP_DECLUTTER_INTERVAL:-1800}")"

if (( FAILURES > 0 )); then
  echo "failed to start $FAILURES loop session(s)" >&2
  exit 1
fi
