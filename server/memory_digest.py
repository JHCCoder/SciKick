"""LLM-based memory digest — the "A" in the A+C hybrid.

Reads pending chat exchanges and distils them into the structured memory fields
(decisions, reviewer_comments, conversation_summary, active_context). Runs on
the periodic sync cadence, off the chat path, so it never adds reply latency.

Defensive by design: if the LLM call fails or returns unparseable JSON, the
pending buffer is left intact and the next tick retries. Pending is only
cleared after a successful apply — data is never silently lost.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from memory_manager import (
    ChatTurn,
    Decision,
    ReviewerCommentState,
    _memory_lock,
    _pending_exchanges,
    _save_local,
    get_current_memory,
)

logger = logging.getLogger("paper-assistant.digest")

# Cap each exchange's text in the prompt to keep token cost tiny even on big
# models. The digest only needs the gist, not the full reply.
_EXCHANGE_CHAR_CAP = 800

_SYSTEM_PROMPT = (
    "You are a precise extraction assistant for a scientific-paper revision "
    "tool. You read chat exchanges and return ONLY valid minified JSON — no "
    "prose, no code fences, no commentary."
)

_INSTRUCTION = """\
You are given NEW chat exchanges between a researcher and an assistant, plus the
CURRENT memory state. Extract what is worth remembering long-term from the NEW
exchanges only.

Return ONLY a JSON object with this exact shape (omit a key if it has no items):
{"decisions":[{"decision":"concise decision the researcher made"}],
 "reviewer_updates":[{"id":"existing comment id","status":"pending|in_progress|resolved|deferred","response_draft":"...","notes":"..."}],
 "key_facts":["durable fact about the paper/project, concise"],
 "summary":"one-line recap of what was discussed"}

Rules:
- decisions: only concrete decisions explicitly made in the exchange.
- reviewer_updates: ONLY for ids that exist in CURRENT MEMORY, and only if the
  exchange actually establishes a status change / response draft / note.
- key_facts: durable facts about the paper or project — NOT chit-chat, not
  pleasantries, not restatements of the exchange.
- summary: a single short line.
- If nothing worth remembering, return {"summary":"one-line recap"}.
"""


def _build_prompt(pending: list[ChatTurn], memory: Any) -> str:
    exchanges = "\n\n".join(
        f"[{t.role}]: {t.content[:_EXCHANGE_CHAR_CAP]}" for t in pending
    )

    decisions = "\n".join(f"- {d.decision}" for d in memory.decisions[-10:]) or "(none)"

    comments = (
        "\n".join(
            f"- {c.id} | {c.status} | {c.text[:120]}"
            for c in memory.reviewer_comments[:25]
        )
        or "(none)"
    )

    return (
        f"NEW CHAT EXCHANGES:\n{exchanges}\n\n"
        f"CURRENT MEMORY — Decisions:\n{decisions}\n\n"
        f"CURRENT MEMORY — Reviewer comments (id | status | text):\n{comments}\n\n"
        f"{_INSTRUCTION}"
    )


async def _call_llm(message: str) -> str:
    """Non-streaming LLM call reusing the chat handler's provider routing."""
    from chat_handler import (
        _get_provider,
        _is_anthropic_provider,
        _sync_anthropic,
        _sync_openai_compatible,
    )

    provider = _get_provider()
    if _is_anthropic_provider(provider["provider"]):
        return await _sync_anthropic(
            message, _SYSTEM_PROMPT, provider["model"], provider["api_key"]
        )
    return await _sync_openai_compatible(
        message,
        _SYSTEM_PROMPT,
        provider["model"],
        provider["api_key"],
        provider["base_url"],
    )


def _parse_json(text: str) -> Optional[dict]:
    """Parse JSON from an LLM response, tolerating surrounding prose/fences."""
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    # Strip markdown code fences if present
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except (json.JSONDecodeError, TypeError):
            pass
    # Last resort: first {...} block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def _apply_parsed(memory: Any, parsed: dict) -> int:
    """Apply parsed digest to memory. Returns number of items applied."""
    now = datetime.now(timezone.utc).isoformat()
    applied = 0

    for d in parsed.get("decisions") or []:
        text = (d.get("decision") if isinstance(d, dict) else str(d)) or ""
        text = text.strip()
        if text:
            memory.decisions.append(Decision(date=now, decision=text[:500]))
            applied += 1

    for u in parsed.get("reviewer_updates") or []:
        if not isinstance(u, dict) or not u.get("id"):
            continue
        uid = str(u["id"])
        for c in memory.reviewer_comments:
            if c.id == uid:
                if u.get("status"):
                    c.status = u["status"]
                if u.get("response_draft"):
                    c.response_draft = u["response_draft"]
                if u.get("notes"):
                    c.notes = u["notes"]
                if u.get("status") == "resolved" and not c.resolved_at:
                    c.resolved_at = now
                applied += 1
                break

    facts = [f for f in (parsed.get("key_facts") or []) if f and str(f).strip()]
    if facts:
        facts_text = "\n".join(f"- {str(f).strip()}" for f in facts)
        existing = memory.active_context.strip()
        memory.active_context = (
            (existing + "\n" + facts_text) if existing else facts_text
        )
        applied += len(facts)

    summary = parsed.get("summary")
    if isinstance(summary, str) and summary.strip():
        memory.conversation_summary = summary.strip()[:1000]
        applied += 1

    return applied


async def digest_pending_exchanges() -> bool:
    """Digest pending exchanges into structured memory.

    Returns True if a digest was performed (pending was non-empty and applied),
    False if there was nothing to digest or the LLM call failed (pending left
    intact for retry).
    """
    # Snapshot pending + memory under the lock, then release for the LLM call.
    async with _memory_lock:
        memory = get_current_memory()
        if memory is None:
            return False
        pending = list(_pending_exchanges)
        if not pending:
            return False
        prompt = _build_prompt(pending, memory)

    # LLM call outside the lock so the event loop / other memory ops aren't blocked.
    try:
        raw = await _call_llm(prompt)
    except Exception as exc:
        logger.warning("Digest LLM call failed (pending kept for retry): %s", exc)
        return False

    parsed = _parse_json(raw)
    if parsed is None:
        logger.warning(
            "Digest returned unparseable JSON (pending kept for retry): %s",
            (raw or "")[:200],
        )
        return False

    # Re-acquire the lock to apply + clear pending.
    async with _memory_lock:
        memory = get_current_memory()
        if memory is None:
            return False
        try:
            applied = _apply_parsed(memory, parsed)
            _pending_exchanges.clear()
            now = datetime.now(timezone.utc).isoformat()
            memory.last_updated = now
            _save_local(memory)
        except Exception as exc:
            logger.error("Digest apply failed (pending kept for retry): %s", exc)
            return False

    logger.info(
        "Digested %d pending exchanges (%d items applied)", len(pending), applied
    )
    return True
