"""Chat handler — multi-provider LLM integration with streaming, context injection.

Supported providers:
  - anthropic (Anthropic SDK)
  - deepseek, openai, custom (OpenAI-compatible SDK)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import get_llm_config
from context_engine import (
    PaperDocument,
    ReviewerComment,
    retrieve_context,
)
from memory_manager import (
    Decision,
    build_resume_context,
    get_current_memory,
    update_memory_after_chat,
    _save_local,
)

logger = logging.getLogger("paper-assistant.chat")
router = APIRouter()

# ---------------------------------------------------------------------------
# Global state for the current project
# ---------------------------------------------------------------------------

_current_doc: Optional[PaperDocument] = None
_current_comments: list[ReviewerComment] = []
_image_cache: dict[str, bytes] = {}  # filename -> raw bytes


def set_project_context(
    doc: PaperDocument,
    comments: list[ReviewerComment],
    images: dict[str, bytes] = None,
) -> None:
    """Set the current project context for chat sessions."""
    global _current_doc, _current_comments, _image_cache
    _current_doc = doc
    _current_comments = comments
    if images:
        _image_cache = images
    logger.info(
        "Project context set: %d sections, %d comments, %d images",
        len(doc.sections),
        len(comments),
        len(_image_cache),
    )


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are PhiDkick — an AI research companion helping a scientist with their academic work. You can assist with brainstorming, scientific writing, manuscript revision, peer review responses, data analysis, and general research discussion.

## Your Role
- Help the researcher think through ideas, develop hypotheses, and plan experiments.
- Provide scientific writing advice: clarity, argument structure, figure presentation, statistical reporting, and effective use of supplementary material.
- When the user is working on revisions, help them understand reviewer comments and formulate clear, persuasive responses.
- Suggest specific revisions to the manuscript that directly address reviewer concerns.
- Check the manuscript text against reviewer comments to identify gaps or needed changes.
- Draft response letter text for specific reviewer points, maintaining a professional and constructive tone.
- Adapt your advice to the user's field — whether it's biology, chemistry, physics, engineering, social sciences, or any other research domain.

## How You Work
- When the user asks about a specific reviewer comment, reference it by ID (e.g., "R2-C3").
- When discussing the paper, cite the relevant section (e.g., "in your Methods section…").
- When relevant, reference specific figures, tables, or supplementary materials by name.
- Be specific and actionable — don't just say "clarify this," suggest HOW to clarify it, with concrete wording or structural suggestions.
- If the user shares their draft response, critique it constructively: is it responsive? respectful? supported by evidence?
- Help prioritise: distinguish between major concerns that require new experiments/analysis and minor points that need clarification or editing.
- If the user is brainstorming or exploring ideas, engage creatively and help them develop their thinking.

## Important
- Never fabricate citations, references, or data that aren't in the paper or user-provided feedback.
- If you're unsure about a domain-specific detail, flag it rather than guess — the user is the expert in their field.
- The user is the domain expert; your job is to help them express their expertise clearly and persuasively.
- Respect the journal's scope and the reviewers' legitimate concerns — don't suggest dismissing valid criticism.

## Context Provided
Each message will include relevant sections of the manuscript and any reviewer comments. Use them to ground your responses in the actual text. If a "Current File" section appears, the user has that specific file open in their browser.

## About This App — PhiDkick
You are the chat interface of a desktop application called **PhiDkick**. Understanding how the app works helps you give accurate answers about its capabilities.

**The app consists of three parts:**
1. **Local server** — runs on the researcher's computer (localhost:8742), handles Google Drive access, file processing, and memory persistence
2. **Chrome extension** — the side panel the researcher is chatting with you through
3. **LLM backend** — that's you, providing the intelligence via API

**What the app does automatically (the researcher doesn't need to ask for this):**
- **Memory is saved after every exchange** — the server writes a `.paper-assistant-memory.json` file to the researcher's Google Drive folder after each of your responses. This file contains the full chat history, reviewer comment statuses, and decisions
- **Cross-computer resume** — if the researcher opens the app on another computer with the same Drive folder, the server downloads the memory file and restores all context. The researcher picks up exactly where they left off
- **Manuscript stays loaded** — once the researcher clicks "Load Project," the server downloads their manuscript and comments from Drive and keeps them in context for the entire session (no need to re-paste)

**What the researcher controls:**
- **⚙ Settings panel** — they can switch LLM providers and models at any time from the gear icon in the extension
- **Which Drive folder** — they paste the Google Drive folder ID to connect their files
- **When to load/reload** — clicking "Load Project" downloads the latest files from Drive

**If the researcher asks about these features:**
- "Can you save this?" / "Do you remember this?" → Explain that the app automatically saves every conversation to their Drive folder as `.paper-assistant-memory.json`, so all progress is persisted
- "Will this be here if I switch computers?" → Yes — the memory file syncs via Google Drive. On any new computer, they just clone the repo, run `./start.sh --setup`, and paste the same Drive folder ID
- "How do I change the model?" → They can click the ⚙ gear icon in the extension's top bar to switch providers and models immediately
"""

RESUME_PROMPT_EXTENSION = """
## Session Resumed
The researcher is continuing a previous session. Below is a summary of where they left off.
"""


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    include_paper_context: bool = True
    include_reviewer_comments: bool = True
    focus_figure: Optional[str] = None
    current_file: Optional[dict] = None  # {name, id} — file the user is viewing in their browser
    session_focus: Optional[str] = None  # brainstorming | paper_discussion | paper_writing | revision | other


class ChatResponse(BaseModel):
    response: str
    context_used: dict = {}


# ---------------------------------------------------------------------------
# Core chat logic
# ---------------------------------------------------------------------------

def _build_system_prompt() -> str:
    """Build the full system prompt including resume context if available."""
    prompt = SYSTEM_PROMPT

    memory = get_current_memory()
    if memory and memory.chat_history:
        prompt += "\n\n" + RESUME_PROMPT_EXTENSION
        prompt += "\n" + build_resume_context()

    return prompt


def _build_user_message(
    message: str,
    include_paper: bool = True,
    include_comments: bool = True,
    focus_figure: Optional[str] = None,
    current_file: Optional[dict] = None,
    session_focus: Optional[str] = None,
) -> str:
    """Build the enriched user message with retrieved context."""
    global _current_doc, _current_comments, _image_cache

    parts = []

    # Session focus — the user's chosen area of work for this session
    if session_focus:
        focus_descriptions = {
            "brainstorming": "The user wants to brainstorm — explore ideas, develop hypotheses, and think creatively. Be expansive and generative.",
            "paper_discussion": "The user wants to discuss their paper — think through results, implications, and narrative. Be analytical and critical.",
            "paper_writing": "The user wants to write — draft, edit, and refine manuscript sections. Be constructive and precise with language.",
            "revision": "The user is working on peer review revisions — address reviewer comments and draft responses. Be systematic and persuasive.",
            "other": "The user has a custom focus. Let them explain and follow their lead.",
        }
        desc = focus_descriptions.get(session_focus, "")
        if desc:
            parts.append(f"## Session Focus: {session_focus}\n{desc}\n")
            parts.append("---\n")

    # Current file the user is viewing
    if current_file and current_file.get("name"):
        parts.append(
            f"## Current File\n"
            f"The user is currently viewing this file from the project: **{current_file['name']}**"
            + (f" (Drive file ID: {current_file['id']})" if current_file.get("id") else "")
            + "\n"
        )
        parts.append("---\n")

    # Figure focus
    if focus_figure and _current_doc:
        try:
            from context_engine import get_figure_context
            fig_context = get_figure_context(focus_figure, _current_doc, _image_cache)
            if fig_context:
                parts.append(
                    f"## Figure Context: {focus_figure}\n"
                    f"Caption: {fig_context['figure']['caption']}\n"
                    f"Section: {fig_context.get('related_section', 'Unknown')}\n"
                )
                parts.append("---\n")
        except Exception:
            pass

    # Retrieved context
    if _current_doc and (include_paper or include_comments):
        memory = get_current_memory()
        chat_history = memory.chat_history if memory else []

        context = retrieve_context(
            query=message,
            doc=_current_doc,
            comments=_current_comments,
            chat_history=[t.model_dump() for t in chat_history],
        )
        if context:
            parts.append(context)
            parts.append("---\n")

    # The user's actual message
    parts.append(f"## User Message\n{message}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Provider-specific streaming implementations
# ---------------------------------------------------------------------------

async def _stream_anthropic(
    message: str, system_prompt: str, model: str, api_key: str
) -> AsyncGenerator[str, None]:
    """Stream using the Anthropic SDK."""
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=api_key)

    try:
        async with client.messages.stream(
            model=model,
            max_tokens=8192,
            system=system_prompt,
            messages=[{"role": "user", "content": message}],
            temperature=0.7,
        ) as stream:
            async for text in stream.text_stream:
                yield f"data: {json.dumps({'type': 'text', 'content': text})}\n\n"

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    except Exception as exc:
        logger.error("Anthropic API error: %s", exc)
        yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"


async def _stream_openai_compatible(
    message: str, system_prompt: str, model: str, api_key: str, base_url: str
) -> AsyncGenerator[str, None]:
    """Stream using the OpenAI-compatible SDK (DeepSeek, OpenAI, Groq, etc.)."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ],
            temperature=0.7,
            max_tokens=8192,
            stream=True,
        )

        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield f"data: {json.dumps({'type': 'text', 'content': delta.content})}\n\n"

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    except Exception as exc:
        logger.error("LLM API error (%s): %s", model, exc)
        yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"


# ---------------------------------------------------------------------------
# Provider-specific sync implementations
# ---------------------------------------------------------------------------

async def _sync_anthropic(
    message: str, system_prompt: str, model: str, api_key: str
) -> str:
    """Non-streaming call via Anthropic SDK."""
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=api_key)
    response = await client.messages.create(
        model=model,
        max_tokens=8192,
        system=system_prompt,
        messages=[{"role": "user", "content": message}],
        temperature=0.7,
    )
    return response.content[0].text


async def _sync_openai_compatible(
    message: str, system_prompt: str, model: str, api_key: str, base_url: str
) -> str:
    """Non-streaming call via OpenAI-compatible SDK."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ],
        temperature=0.7,
        max_tokens=8192,
    )
    return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Provider routing
# ---------------------------------------------------------------------------

def _is_anthropic_provider(provider: str) -> bool:
    return provider == "anthropic"


def _get_provider() -> dict:
    """Get the current LLM provider config, raising a friendly error on failure."""
    try:
        return get_llm_config()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/send")
async def send_message(req: ChatRequest):
    """Send a message to the revision assistant (streaming SSE)."""
    provider = _get_provider()

    system_prompt = _build_system_prompt()
    user_message = _build_user_message(
        message=req.message,
        include_paper=req.include_paper_context,
        include_comments=req.include_reviewer_comments,
        focus_figure=req.focus_figure,
        current_file=req.current_file,
        session_focus=req.session_focus,
    )

    logger.info(
        "Chat request [%s/%s]: '%s...' (system=%d, user=%d chars)",
        provider["provider"],
        provider["model"],
        req.message[:80],
        len(system_prompt),
        len(user_message),
    )

    if _is_anthropic_provider(provider["provider"]):
        stream = _stream_anthropic(
            user_message, system_prompt, provider["model"], provider["api_key"]
        )
    else:
        stream = _stream_openai_compatible(
            user_message,
            system_prompt,
            provider["model"],
            provider["api_key"],
            provider["base_url"],
        )

    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/send-sync")
async def send_message_sync(req: ChatRequest):
    """Send a message and get a complete (non-streaming) response."""
    provider = _get_provider()

    system_prompt = _build_system_prompt()
    user_message = _build_user_message(
        message=req.message,
        include_paper=req.include_paper_context,
        include_comments=req.include_reviewer_comments,
        focus_figure=req.focus_figure,
        current_file=req.current_file,
        session_focus=req.session_focus,
    )

    try:
        if _is_anthropic_provider(provider["provider"]):
            assistant_text = await _sync_anthropic(
                user_message, system_prompt, provider["model"], provider["api_key"]
            )
        else:
            assistant_text = await _sync_openai_compatible(
                user_message,
                system_prompt,
                provider["model"],
                provider["api_key"],
                provider["base_url"],
            )
    except Exception as exc:
        logger.error("LLM API error: %s", exc)
        raise HTTPException(status_code=502, detail=f"LLM API error: {exc}")

    # Update memory
    update_memory_after_chat(
        user_message=req.message,
        assistant_message=assistant_text,
    )

    return {
        "response": assistant_text,
        "context_used": {
            "provider": provider["provider"],
            "model": provider["model"],
            "system_prompt_length": len(system_prompt),
            "user_message_length": len(user_message),
            "paper_loaded": _current_doc is not None,
            "comments_loaded": len(_current_comments),
        },
    }


class ConfigureRequest(BaseModel):
    provider: str = ""      # "anthropic" | "deepseek" | "openai" | "custom"
    api_key: str = ""
    model: str = ""
    base_url: str = ""      # only for custom
    persist: bool = True    # save to .env for next restart


@router.post("/configure")
async def configure_llm(req: ConfigureRequest):
    """Change the LLM provider/model at runtime."""
    from config import set_llm_config, _save_runtime_config_to_env

    set_llm_config(
        provider=req.provider or None,
        model=req.model or None,
        api_key=req.api_key or None,
        base_url=req.base_url or None,
    )

    if req.persist:
        try:
            _save_runtime_config_to_env()
        except Exception as exc:
            logger.warning("Failed to persist config to .env: %s", exc)

    current = get_llm_config()
    return {
        "status": "configured",
        "current": {
            "provider": current["provider"],
            "model": current["model"],
            "configured": True,
        },
    }


@router.get("/providers")
async def list_providers():
    """Return information about available and configured providers."""
    current = None
    try:
        current = get_llm_config()
    except Exception:
        pass

    return {
        "current": {
            "provider": current["provider"] if current else "unknown",
            "model": current["model"] if current else "unknown",
            "configured": current is not None,
        } if current else None,
        "available": [
            {
                "id": "anthropic",
                "name": "Anthropic (Claude)",
                "sdk": "Anthropic SDK",
                "models": "claude-sonnet-4-6, claude-opus-4-8, claude-haiku-4-5, etc.",
                "env_vars": "LLM_API_KEY or ANTHROPIC_API_KEY",
            },
            {
                "id": "deepseek",
                "name": "DeepSeek",
                "sdk": "OpenAI-compatible",
                "models": "deepseek-chat, deepseek-reasoner",
                "env_vars": "LLM_API_KEY or DEEPSEEK_API_KEY",
            },
            {
                "id": "openai",
                "name": "OpenAI (GPT-4o, etc.)",
                "sdk": "OpenAI SDK",
                "models": "gpt-4o, gpt-4-turbo, gpt-3.5-turbo, etc.",
                "env_vars": "LLM_API_KEY or OPENAI_API_KEY",
            },
            {
                "id": "custom",
                "name": "Custom (OpenAI-compatible)",
                "sdk": "OpenAI-compatible SDK",
                "models": "Any model your provider supports",
                "env_vars": "LLM_API_KEY + LLM_BASE_URL (required)",
            },
        ],
    }


# ---------------------------------------------------------------------------
# Context window tracking
# ---------------------------------------------------------------------------

# Approximate context window sizes per model (in tokens)
MODEL_CONTEXT_WINDOWS = {
    "deepseek-v4-pro": 131072,
    "deepseek-v4-flash": 131072,
    "deepseek-chat": 65536,
    "deepseek-reasoner": 65536,
    "claude-sonnet-4-6": 200000,
    "claude-opus-4-8": 200000,
    "claude-haiku-4-5": 200000,
    "gpt-4o": 128000,
    "gpt-4-turbo": 128000,
}


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _get_context_window_size() -> tuple[int, str]:
    """Return the context window size for the current model."""
    try:
        cfg = get_llm_config()
        model = cfg.get("model", "")
        size = MODEL_CONTEXT_WINDOWS.get(model, 131072)
        return size, model
    except Exception:
        return 131072, "unknown"


@router.get("/context-usage")
async def context_usage():
    """
    Estimate current context window usage per message.

    The full manuscript is NOT sent every message — only the top 3 most
    relevant chunks are injected via keyword retrieval. This endpoint
    estimates what's actually sent to the LLM on a typical turn.
    """
    window_size, model = _get_context_window_size()

    system_tokens = _estimate_tokens(SYSTEM_PROMPT)
    resume_tokens = _estimate_tokens(RESUME_PROMPT_EXTENSION)

    # Retrieval injects ~3 chunks of up to 4000 chars each + ~5 comments
    retrieval_chunks_estimate = 3 * 4000 // 4  # ~3000 tokens
    retrieval_comments_estimate = 5 * 500 // 4   # ~625 tokens
    retrieval_tokens = retrieval_chunks_estimate + retrieval_comments_estimate

    # Chat history tokens (capped at CHAT_HISTORY_LIMIT turns)
    history_tokens = 0
    memory = get_current_memory()
    if memory and memory.chat_history:
        history_tokens = sum(
            _estimate_tokens(t.content) for t in memory.chat_history
        )

    # Current message + response reserve
    message_reserve = 8000  # ~2000 tokens for message + 6000 for response

    total_used = (
        system_tokens
        + resume_tokens
        + retrieval_tokens
        + history_tokens
        + message_reserve
    )

    pct_used = round(min((total_used / window_size) * 100, 100), 1)
    remaining = max(window_size - total_used, 0)

    return {
        "model": model,
        "window_size": window_size,
        "breakdown": {
            "system_prompt": system_tokens,
            "retrieval_chunks": retrieval_chunks_estimate,
            "chat_history": history_tokens,
            "message_reserve": message_reserve,
        },
        "total_used": total_used,
        "remaining": remaining,
        "pct_used": pct_used,
        "pct_free": round(100 - pct_used, 1),
        "manuscript_available": _current_doc is not None,
        "manuscript_total_chars": len(_current_doc.full_text) if _current_doc else 0,
    }


@router.post("/refresh-context")
async def refresh_context():
    """
    Save the current conversation summary to memory, clear chat history,
    and return the freed context window.

    Useful when the context window is filling up — important decisions
    and comment statuses are preserved in the memory file on Drive.
    """
    memory = get_current_memory()
    if memory is None:
        return {"status": "no_memory", "message": "No active session to refresh."}

    # Save a snapshot of the current conversation state
    now = datetime.now(timezone.utc).isoformat()

    # Summarise what was discussed
    if memory.chat_history:
        user_messages = [t for t in memory.chat_history if t.role == "user"]
        topics = [t.content[:200] for t in user_messages[-5:]]
        summary = "Topics discussed:\n" + "\n".join(f"- {t}" for t in topics)
    else:
        summary = "No conversation to summarise."

    memory.conversation_summary = summary
    memory.last_updated = now

    # Build a compact decision log from recent chat
    recent_decisions = []
    for t in memory.chat_history[-10:]:
        if t.role == "assistant" and any(
            kw in t.content.lower()
            for kw in ["decided", "decision", "agreed", "we will", "let's", "plan:"]
        ):
            recent_decisions.append(
                Decision(date=now, decision=t.content[:500])
            )
    if recent_decisions:
        memory.decisions.extend(recent_decisions)

    # Save the old turn count for the response
    old_turns = len(memory.chat_history)

    # Clear chat history to free context window
    memory.chat_history = []

    # Save locally and sync to Drive
    _save_local(memory)
    if memory.project_folder_id:
        try:
            from drive_sync import _save_memory_to_drive
            _save_memory_to_drive(memory.project_folder_id, memory.model_dump())
        except Exception as exc:
            logger.warning("refresh-context: Drive sync failed: %s", exc)

    # Return new context usage
    usage = await context_usage()

    return {
        "status": "refreshed",
        "turns_cleared": old_turns,
        "decisions_saved": len(recent_decisions),
        "summary": summary[:500],
        "context": usage,
    }


@router.get("/context")
async def get_context():
    """Get a summary of the current project context."""
    if _current_doc is None:
        return {"loaded": False, "paper": None, "comments": [], "images": []}

    return {
        "loaded": True,
        "paper": {
            "title": _current_doc.title,
            "sections": [s.heading for s in _current_doc.sections],
            "figures": [f.filename for f in _current_doc.figures],
            "full_text_length": len(_current_doc.full_text),
        },
        "comments": [
            {
                "id": c.id,
                "reviewer": c.reviewer,
                "severity": c.severity,
                "text_preview": c.text[:200],
                "related_sections": c.related_sections,
                "related_figures": c.related_figures,
            }
            for c in _current_comments
        ],
        "images": list(_image_cache.keys()),
    }
