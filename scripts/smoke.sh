#!/usr/bin/env bash
# Live smoke test: one engine, all three roles on Haiku, in a temporary git repo under /tmp.
# Usage: scripts/smoke.sh claude-code|opencode
# Costs a handful of Haiku calls. It never deletes anything; clean up with the commands it prints.
set -u
engine="${1:?usage: scripts/smoke.sh claude-code|opencode}"
case "$engine" in
  claude-code) model="claude-haiku-4-5-20251001" ;;
  opencode) model="anthropic/claude-haiku-4-5-20251001" ;;
  *) echo "engine must be claude-code or opencode" >&2; exit 2 ;;
esac
here="$(cd "$(dirname "$0")/.." && pwd)"
bridge="$here/.venv/bin/agent-bridge"

if pgrep -fl "tools/bridge.py" >/dev/null; then
  echo "SKIPPED: a tools/bridge.py process is running:"; pgrep -fl "tools/bridge.py"; exit 3
fi

stamp="$(date +%Y%m%d-%H%M%S)"
repo="/tmp/agent-bridge-smoke-$engine-$stamp"
state="/tmp/agent-bridge-smoke-state-$stamp"
mkdir -p "$repo" "$state"
git -C "$repo" init -q
git -C "$repo" config user.name "Smoke Test"
git -C "$repo" config user.email "smoke@example.invalid"
export XDG_STATE_HOME="$state"
run() { env -u ANTHROPIC_API_KEY "$bridge" "$@" --repo "$repo" </dev/null; }

idea="A Python module wordcount.py with one function count_words(text: str) -> int that counts whitespace-separated words, and a pytest file test_wordcount.py with three tests. Nothing else: no packaging, no CLI."
echo "== repo: $repo"
echo "== 1. new (stops at the planner's questions without a terminal)"
run new "$idea" --planner "$engine:$model" --supervisor "$engine:$model" --builder "$engine:$model"; echo "exit=$?"
echo "== 2. say: use your recommendations (the planner drafts the contract)"
run say "use your recommendations"; echo "exit=$?"
echo "== 3. approve and run two exchanges"
run approve --loop 2; echo "exit=$?"
echo "== 4. status"
run status
echo "== 5. sessions created (delete only these, by exact id, after checking them)"
cat "$repo/.bridge/sessions.json" 2>/dev/null
echo
echo "repo=$repo"
echo "state=$state"
