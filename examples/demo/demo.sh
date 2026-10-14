#!/usr/bin/env bash
# Zero-quota demo of Agent Landing: a budget opens, LAND denies a write, the
# final reply is saved as the handoff.
#
# It feeds the real hook commands the same JSON payloads Claude Code sends, in a
# throwaway LANDING_HOME, so no model runs and no account quota is spent.
#
#   asciinema rec -c "bash examples/demo/demo.sh" landing-demo.cast
#   agg landing-demo.cast landing-demo.gif      # optional GIF
#
# Env: LANDING_PY  python that has agent-landing installed (default: python)
#      DEMO_PAUSE  seconds between steps (default: 1.5; 0 for a dry run)
set -euo pipefail

PY="${LANDING_PY:-python}"
PAUSE="${DEMO_PAUSE:-1.5}"
LANDING_HOME="$(mktemp -d)"
export LANDING_HOME
trap 'rm -rf "$LANDING_HOME"' EXIT

landing() { "$PY" -m agent_landing.cli "$@"; }
say()  { printf '\n\033[1;36m# %s\033[0m\n' "$*"; sleep "$PAUSE"; }
show() { printf '\033[2m$ %s\033[0m\n' "$*"; sleep "$PAUSE"; }
# Print only the fields a viewer cares about from a hook's JSON reply.
field() { "$PY" -c '
import json, sys
data = json.load(sys.stdin) or {}
out = data.get("hookSpecificOutput", data)
for key in sys.argv[1:]:
    if out.get(key):
        print(f"{key}: {out[key]}")
if not any(out.get(k) for k in sys.argv[1:]):
    print("(no objection: tool runs)")
' "$@"; }

now=$(date +%s)
session='"session_id":"demo"'

say "Quota as Claude Code reports it to the status line: 88% of the 5h window, 73% of the week left."
show 'claude-statusline  <  {"rate_limits": {...}}'
printf '{%s,"rate_limits":{"five_hour":{"used_percentage":12,"resets_at":%d},"seven_day":{"used_percentage":27,"resets_at":%d}}}' \
  "$session" $((now + 9000)) $((now + 400000)) | landing claude-statusline

say "The user types a budget. The floor (72%) sits just under the weekly reading, so the landing boundary (floor + 4% reserve) is already reached."
show '/landing leave 72 fix the tests'
printf '{%s,"hook_event_name":"UserPromptSubmit","prompt":"/landing leave 72 fix the tests","cwd":"."}' "$session" \
  | landing hook-user-prompt --platform claude \
  | "$PY" -c 'import json,sys; c=json.load(sys.stdin)["hookSpecificOutput"]["additionalContext"]; print(c.split(" That floor")[0])'

say "The agent tries to edit a file. The PreToolUse gate denies it."
show 'Edit src/app.py'
printf '{%s,"hook_event_name":"PreToolUse","tool_name":"Edit","tool_input":{"file_path":"src/app.py","old_string":"a","new_string":"b"},"cwd":"."}' "$session" \
  | landing hook-pre-tool --platform claude | field permissionDecision permissionDecisionReason

say "Reading is still allowed, so the handoff can be accurate."
show 'Read src/app.py'
printf '{%s,"hook_event_name":"PreToolUse","tool_name":"Read","tool_input":{"file_path":"src/app.py"},"cwd":"."}' "$session" \
  | landing hook-pre-tool --platform claude | field permissionDecision

say "The agent cannot lift its own floor either."
show 'Bash: landing cancel'
printf '{%s,"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"landing cancel"},"cwd":"."}' "$session" \
  | landing hook-pre-tool --platform claude | field permissionDecision permissionDecisionReason

say "Its final reply is the handoff. The Stop hook saves it outside the project."
reply='## Handoff\n- Task: fix the tests\n- Done: located the failing assertion in tests/test_app.py\n- Next: update the fixture, rerun pytest\n- Resume: \"Continue fixing tests/test_app.py; the fixture is stale.\"'
printf '{%s,"hook_event_name":"Stop","last_assistant_message":"%s","cwd":"."}' "$session" "$reply" \
  | landing hook-stop --platform claude | field systemMessage
cat "$LANDING_HOME"/handoffs/*.md

say "Done. No model ran and no quota was spent."
