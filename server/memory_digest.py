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
- NEVER record that a document was "scanned", "kept", "loaded", "focused", or
  "added to context" — in decisions, key_facts, OR summary. That state is
  ephemeral (server memory only, cleared on restart) and is NOT restored on
  resume, so recording it would mislead future sessions into thinking the file
  is already loaded. Scanning/keeping a file is a routine operation, not a
  durable fact worth remembering.
- summary: a single short line about the substantive topic discussed — never
  enumerate which files were scanned or kept.
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


_SUMMARY_SYSTEM_PROMPT = (
    "You write extremely concise research-project summaries. Output ONLY the "
    "summary prose — no preamble, no labels, no markdown. At most two sentences."
)

# Cap each sampled excerpt so the prompt stays tiny (this runs on every Load
# Project). We only need enough to characterize the project.
_SAMPLE_CHAR_CAP = 1200


async def generate_project_summary(
    title: str, samples: list[tuple[str, str]]
) -> str:
    """Infer a ≤2-sentence project summary from the manuscript title + excerpts.

    ``samples`` is a list of (label, text) tuples — e.g. the first chunk of
    each parsed project file. Reuses the chat handler's provider routing
    (same provider/model the user configured for chat). Raises on failure so
    the caller can fall back to the title/filename.
    """
    from chat_handler import (
        _get_provider,
        _is_anthropic_provider,
        _sync_anthropic,
        _sync_openai_compatible,
    )

    excerpts = []
    for label, text in samples:
        text = (text or "").strip()
        if not text:
            continue
        excerpts.append(f"[{label}]\n{text[:_SAMPLE_CHAR_CAP]}")
    excerpt_block = "\n\n".join(excerpts[:8]) or "(no excerpts available)"

    message = (
        f"Manuscript title: {title or '(unknown)'}\n\n"
        f"File excerpts:\n{excerpt_block}\n\n"
        "In at most two sentences, summarize what this research project is "
        "about — the topic, organism/system, and main question or contribution "
        "— as inferred from the title and excerpts. Be specific and succinct. "
        "Output only the summary."
    )

    provider = _get_provider()
    if _is_anthropic_provider(provider["provider"]):
        text = await _sync_anthropic(
            message, _SUMMARY_SYSTEM_PROMPT, provider["model"], provider["api_key"]
        )
    else:
        text = await _sync_openai_compatible(
            message,
            _SUMMARY_SYSTEM_PROMPT,
            provider["model"],
            provider["api_key"],
            provider["base_url"],
        )
    text = (text or "").strip()
    if not text:
        raise ValueError("empty summary from LLM")
    return text


# ---------------------------------------------------------------------------
# Context lookup — journals, programs, grants (paper / application / grant modes)
# ---------------------------------------------------------------------------

# A best-effort lookup that gathers reference info for the user's stated target
# (a journal's author guidelines, a school/company program, or a grant
# mechanism) and returns a source URL. Hybrid: try a web scrape first (genuine
# fetch, citing the scraped page's URL), then fall back to an LLM-knowledge
# lookup that also cites a canonical source URL — so we get notes + a URL for
# known targets even when the site is JS-heavy and unscrapable. Never raises.
_LOOKUP_CONFIG = {
    "journal": {
        "query": "{name} author guidelines instructions for authors",
        "distill_sys": (
            "You extract concise author-formatting guidelines for scientific "
            "journals. Output ONLY a short bulleted markdown list (<=10 lines) "
            "of the PRACTICAL rules (word/figure/table limits, abstract "
            "structure & length, section headings, reference style). Be specific "
            "with numbers. If the text is not real author-guidelines content, "
            "output exactly: NO GUIDELINES FOUND"
        ),
        "distill_instr": (
            "Journal: {name}\n\nBelow is text scraped from a page that may be "
            "this journal's author guidelines. Distil the practical formatting "
            "rules. Ignore navigation, ads, and boilerplate. If it is not "
            "actually author guidelines, output: NO GUIDELINES FOUND\n\n"
            "Scraped text:\n{body}"
        ),
        "fallback_sys": (
            "You provide concise author-formatting guidelines for scientific "
            "journals from your training knowledge. Output a short bulleted "
            "markdown list (<=10 lines) of the practical rules, then on a final "
            "line beginning with 'SOURCE: ' give the canonical URL of this "
            "journal's instructions-for-authors page. If you do not genuinely "
            "know this journal, output only: NO GUIDELINES FOUND"
        ),
        "fallback_instr": (
            "Journal: {name}\n\nFrom your knowledge, provide the practical "
            "author-formatting guidelines for this journal (word/figure/table "
            "limits, abstract structure, reference style, etc.). Then end with a "
            "line: SOURCE: <canonical instructions-for-authors URL>"
        ),
        "not_found": "NO GUIDELINES FOUND",
    },
    "program": {
        "query": "{name} admissions requirements curriculum mission",
        "distill_sys": (
            "You extract concise application/program info for school or company "
            "programs. Output ONLY a short bulleted markdown list (<=10 lines): "
            "mission/focus, prerequisites, what the program looks for, "
            "application components, key dates. If not real program content, "
            "output exactly: NO INFO FOUND"
        ),
        "distill_instr": (
            "Target program/institution: {name}\n\nBelow is text scraped from a "
            "page about this program. Distil what an applicant should know. "
            "Ignore navigation, ads, and boilerplate. If not relevant, output: "
            "NO INFO FOUND\n\nScraped text:\n{body}"
        ),
        "fallback_sys": (
            "You provide concise application/program info from your training "
            "knowledge. Output a short bulleted markdown list (<=10 lines): "
            "mission/focus, what it looks for in applicants, prerequisites, "
            "notable requirements. Then on a final line beginning with "
            "'SOURCE: ' give the canonical URL of this program's admissions/info "
            "page. If you do not genuinely know it, output only: NO INFO FOUND"
        ),
        "fallback_instr": (
            "Target program/institution: {name}\n\nFrom your knowledge, provide "
            "concise info about this as an application target: mission/focus, "
            "what it looks for, prerequisites, notable requirements. Then end "
            "with a line: SOURCE: <canonical admissions/info URL>"
        ),
        "not_found": "NO INFO FOUND",
    },
    "grant": {
        "query": "{name} grant mechanism review criteria specific aims",
        "distill_sys": (
            "You extract concise grant-program info. Output ONLY a short bulleted "
            "markdown list (<=10 lines): mechanism, review criteria, application "
            "structure (specific aims etc.), page/section limits, funding scope. "
            "If not real grant content, output exactly: NO INFO FOUND"
        ),
        "distill_instr": (
            "Grant: {name}\n\nBelow is text scraped from a page about this grant. "
            "Distil what an applicant should know. Ignore navigation, ads, and "
            "boilerplate. If not relevant, output: NO INFO FOUND\n\n"
            "Scraped text:\n{body}"
        ),
        "fallback_sys": (
            "You provide concise grant-program info from your training knowledge. "
            "Output a short bulleted markdown list (<=10 lines): mechanism, review "
            "criteria, application structure, key limits. Then on a final line "
            "beginning with 'SOURCE: ' give the canonical URL of this grant's "
            "program page. If you do not genuinely know it, output only: NO INFO FOUND"
        ),
        "fallback_instr": (
            "Grant: {name}\n\nFrom your knowledge, provide concise info about "
            "this grant: mechanism, review criteria, application structure "
            "(specific aims etc.), key limits. Then end with a line: SOURCE: "
            "<canonical program URL>"
        ),
        "not_found": "NO INFO FOUND",
    },
}


async def _llm_call(message: str, system_prompt: str) -> str:
    """Non-streaming LLM call with a custom system prompt (reuses the chat
    handler's provider routing). Returns "" on failure."""
    try:
        from chat_handler import (
            _get_provider,
            _is_anthropic_provider,
            _sync_anthropic,
            _sync_openai_compatible,
        )

        provider = _get_provider()
        if _is_anthropic_provider(provider["provider"]):
            return await _sync_anthropic(
                message, system_prompt, provider["model"], provider["api_key"]
            )
        return await _sync_openai_compatible(
            message, system_prompt, provider["model"],
            provider["api_key"], provider["base_url"],
        )
    except Exception as exc:
        logger.warning("LLM call failed: %s", exc)
        return ""


def _parse_source_url(text: str) -> tuple[str, str]:
    """Split a trailing 'SOURCE: <url>' line from the notes body.

    Returns (notes, url); url is "" when no SOURCE line is present.
    """
    lines = (text or "").rstrip().splitlines()
    url = ""
    while lines:
        last = lines[-1].strip()
        if last.upper().startswith("SOURCE:"):
            rest = last.split(":", 1)[1].strip()
            # Take the first http(s) token (in case of surrounding prose).
            url = next((t for t in rest.split() if t.startswith("http")), "")
            lines.pop()
            break
        if not last:
            lines.pop()
            continue
        break
    notes = "\n".join(lines).strip()
    return notes, url


async def fetch_context_info(kind: str, name: str) -> tuple[str, bool, str]:
    """Best-effort lookup of reference info for a journal / program / grant.

    Returns (notes, ok, source_url). Uses the configured chat LLM's knowledge
    to produce concise notes AND a canonical source URL for the target. This is
    preferred over live web-scraping because journal/program sites are
    typically JS-heavy and unscrapable, and scraping each candidate page via an
    LLM distil call is slow (>20s) and usually yields nothing. ``ok`` is True
    when notes were obtained; ``source_url`` is "" if the model provided none.
    Never raises.
    """
    cfg = _LOOKUP_CONFIG.get(kind)
    name = (name or "").strip()
    if not cfg or not name:
        return "", False, ""

    message = cfg["fallback_instr"].format(name=name[:200])
    text = await _llm_call(message, cfg["fallback_sys"])
    text = (text or "").strip()
    if not text or cfg["not_found"] in text.upper():
        logger.info("Context lookup (%s) found nothing for '%s'", kind, name)
        return "", False, ""
    notes, url = _parse_source_url(text)
    if not notes:
        return "", False, ""
    logger.info(
        "Context lookup (%s) LLM-knowledge OK for '%s' (url=%s)",
        kind, name, url or "(none)",
    )
    return notes, True, url


async def fetch_journal_formatting(journal: str) -> tuple[str, bool, str]:
    """Back-compat alias for the original journal-only entry point."""
    return await fetch_context_info("journal", journal)



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
