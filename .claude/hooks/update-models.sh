#!/usr/bin/env bash
#
# UserPromptSubmit hook — refreshes SciKick's LLM provider model tables when the
# submitted prompt contains the phrase "update models".
#
# Wired up from .claude/settings.json. Reads the hook JSON (with the submitted
# prompt) on stdin. On a match, spawns a headless `claude -p` subagent in the
# repo root that follows .claude/skills/update-models/SKILL.md, then blocks the
# current turn and reports the subagent's summary.
set -euo pipefail

# Recursion guard: the headless subagent we spawn sets this env var so its own
# UserPromptSubmit hook (same project settings) becomes a no-op.
if [ -n "${SCIKICK_UPDATE_MODELS:-}" ]; then
  exit 0
fi

# Only react to prompts containing the magic phrase. Exit silently otherwise so
# every other prompt flows through untouched.
if ! jq -r '.prompt // ""' | grep -qi "update models"; then
  exit 0
fi

# Resolve the repo root (the session cwd may be a subdirectory). Skip quietly if
# we're somehow not inside the repo.
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$ROOT"

# Spawn the headless subagent. The prompt deliberately avoids the literal phrase
# "update models" (the env guard above is the real protection, this is belt-and-
# suspenders). Broad --allowedTools are intentional: the subagent needs to run
# py_compile/pytest/git and web research. It only ever fires on the user's own
# "update models" prompt.
OUT="$(SCIKICK_UPDATE_MODELS=1 claude -p \
  "Follow .claude/skills/update-models/SKILL.md exactly to refresh the SciKick LLM provider model tables. When finished, print a one-paragraph summary of what changed and the commit hash." \
  --allowedTools Read \
  --allowedTools Edit \
  --allowedTools Write \
  --allowedTools Bash \
  --allowedTools WebSearch \
  --allowedTools WebFetch \
  2>&1)" || true

# Block the current turn and surface the subagent's summary (truncated).
SUMMARY="$(printf '%s' "$OUT" | tail -c 1500)"
printf '{"decision":"block","reason":%s}' "$(printf '%s' "$SUMMARY" | jq -Rs .)"
