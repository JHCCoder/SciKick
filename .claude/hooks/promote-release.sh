#!/usr/bin/env bash
#
# UserPromptSubmit hook — runs the release promotion when the user reports that
# a submitted version was accepted on the Chrome Web Store.
#
# Wired up from .claude/settings.json. Reads the hook JSON (with the submitted
# prompt) on stdin. On a match, spawns a headless `claude -p` subagent in the
# repo root that follows .claude/skills/promote-release/SKILL.md, then blocks
# the current turn and reports the subagent's summary.
set -euo pipefail

# Recursion guard: the headless subagent we spawn sets this env var so its own
# UserPromptSubmit hook (same project settings) becomes a no-op.
if [ -n "${SCIKICK_PROMOTE_RELEASE:-}" ]; then
  exit 0
fi

# Only react to prompts reporting store acceptance/approval. Exit silently
# otherwise so every other prompt flows through untouched.
if ! jq -r '.prompt // ""' | grep -qiE "(web[ -]?store|webstore|the store|chrome|google).{0,30}(accepted|approved|published)|(accepted|approved|published).{0,30}(web[ -]?store|webstore|the store)|(is|went|goes|just went) live (on|in)? ?(the )?(web[ -]?store|webstore|store)|0?\.[0-9]+ ?(was|is|got|has been)? ?(accepted|approved|published)|version ?(was|is|has been|got)? ?(accepted|approved|published)"; then
  exit 0
fi

# Resolve the repo root (the session cwd may be a subdirectory). Skip quietly if
# we're somehow not inside the repo.
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$ROOT"

# Spawn the headless subagent. The env guard above is the real recursion
# protection (belt-and-suspenders: the prompt avoids the trigger phrases).
OUT="$(SCIKICK_PROMOTE_RELEASE=1 claude -p \
  "Follow .claude/skills/promote-release/SKILL.md exactly to promote the store-accepted SciKick version. When finished, print a one-paragraph summary: the accepted version, tag, draft release URL, the new main commit, and the next step (review + publish the draft)." \
  --allowedTools Read \
  --allowedTools Edit \
  --allowedTools Write \
  --allowedTools Bash \
  --allowedTools Grep \
  2>&1)" || true

# Block the current turn and surface the subagent's summary (truncated).
SUMMARY="$(printf '%s' "$OUT" | tail -c 2000)"
printf '{"decision":"block","reason":%s}' "$(printf '%s' "$SUMMARY" | jq -Rs .)"
