---
name: update-models
description: Refresh SciKick's LLM provider model lists + context windows so the newest models are selectable by any user. Triggered by the "update models" phrase (hook) or /update-models. Edits server/chat_handler.py, verifies, and commits.
---

# Update SciKick LLM Models

Models ship fast; this refreshes the model tables SciKick exposes so the newest
models are usable by any user. Run it monthly/quarterly. Safe to run repeatedly.

## What to change

All edits live in `server/chat_handler.py`:

1. **`_PROVIDER_MODELS`** (~line 2208) — `dict[provider, "id1, id2, …"]`. This is
   the **single source of truth** for model IDs: it feeds `/chat/providers`
   (settings hint) and the invalid-model error suggestions.
2. **`MODEL_CONTEXT_WINDOWS`** (~line 3107) — `dict[model, context_tokens]`.
   Powers the context meter and per-doc character budgets. Default fallback is
   131072 tokens.

Leave the `local-*` and `custom` entries free-form. `/chat/providers` needs no
editing — its `models` string is derived from `_PROVIDER_MODELS`.

## Research current models + context windows

For each remote provider — anthropic, deepseek, glm, openai, gemini, kimi,
grok, minimax, qwen — fetch the CURRENT official model lis
context window from the provider's docs (preferred) or `/models` API:

| Provider  | Source |
|-----------|--------|
| anthropic | https://docs.anthropic.com/en/docs/about-clapic.com/v1/models`) |
| deepseek  | https://api-docs.deepseek.com |
| glm       | https://docs.z.ai or https://open.bigmodel.c
| openai    | https://platform.openai.com/docs/models |
| gemini    | https://ai.google.dev/gemini-api/docs/models
| kimi      | https://platform.moonshot.ai/docs |
| grok      | https://docs.x.ai/docs/models |
| minimax   | https://platform.minimaxi.com/document/Models |
| qwen      | https://help.aliyun.com/zh/model-studio/mode

If a provider exposes a `/models` endpoint and a key is pr
may call it — but **never print, log, or commit keys**. Pr
exact context windows are often absent from the models API

## Rules

- Curated list: the current notable models per provider, n
- Model IDs are case-sensitive — copy them exactly.
- Every remote model in `_PROVIDER_MODELS` needs a correct
  `MODEL_CONTEXT_WINDOWS` entry; add new ones and drop ent
  models.
- Keep list strings readable: `"a, b, c"`.
- If a provider can't be verified, leave it unchanged and
  summary.

## Verify

```bash
./.venv/bin/python -m py_compile server/chat_handler.py
./.venv/bin/python -m pytest server/tests/ -q
```

Tests reference models like `gpt-4o` / `claude-sonnet-4-6`
listed model, check whether any test uses it.

## Commit

Stage ONLY the files you changed (normally just `server/ch
commit:

```bash
git add server/chat_handler.py
git commit -m "feat(chat): refresh provider model lists +
```

Do **not** push — the user syncs branches across machines

## Report

End with a one-paragraph summary: providers touched, model
context windows updated, test results, and the commit hash

Tell me when it's saved, and I'll verify everything and coommit
