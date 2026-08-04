#!/usr/bin/env bash
#
# UserPromptSubmit hook — runs the "prepare for Web Store submission" workflow
# when the submitted prompt asks to prepare/submit the current version.
#
# Wired up from .claude/settings.json. Reads the hook JSON (with the submitted
# prompt) on stdin. On a match, spawns a headless `claude -p` subagent in the
# repo root that follows .claude/skills/prepare-submission/SKILL.md, then blocks
# the current turn and reports the subagent's summary.
set -euo pipefail

# Recursion guard: the headless subagent we spawn sets this env var so its own
# UserPromptSubmit hook (same project settings) becomes a no-op.
if [ -n "${SCIKICK_PREPARE_SUBMISSION:-}" ]; then
  exit 0
fi

# Only react to prompts about preparing for / submitting to the Web Store.
# Exit silently otherwise so every other prompt flows through untouched.
if ! jq -r '.prompt // ""' | grep -qiE "prepare[^.]*(web[ -]?store|webstore|submission)|(submit|submission)[^.]*(web[ -]?store|webstore)|(web[ -]?store|webstore) submission"; then
  exit 0
fi

# Resolve the repo root (the session cwd may be a subdirectory). Skip quietly if
# we're somehow not inside the repo.
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$ROOT"

# Spawn the headless subagent. The env guard above is the real recursion
# protection (belt-and-suspenders: the prompt avoids the trigger phrases).
OUT="$(SCIKICK_PREPARE_SUBMISSION=1 claude -p \
  "Follow .claude/skills/prepare-submission/SKILL.md exactly to prepare the current SciKick version for Chrome Web Store submission. When finished, print a one-paragraph summary: the version, README/description changes, any version bump and its semver rationale, the commit hash(es), and the zip path." \
  --allowedTools Read \
  --allowedTools Edit \
  --allowedTools Write \
  --allowedTools Bash \
  --allowedTools Grep \
  --allowedTools WebSearch \
  --allowedTools WebFetch \
  2>&1)" || true

# Block the current turn and surface the subagent's summary (truncated).
SUMMARY="$(printf '%s' "$OUT" | tail -c 2000)"
printf '{"decision":"block","reason":%s}' "$(printf '%s' "$SUMMARY" | jq -Rs .)"
