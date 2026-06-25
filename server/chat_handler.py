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
_current_doc_source: str = ""  # "drive:<folder_id>"

# Web-scraped papers — accumulate (multiple allowed), separate from Drive context
_scraped_docs: list[PaperDocument] = []
_scraped_sources: list[str] = []  # URLs, parallel to _scraped_docs

# Focused file cache — file_id → parsed text content
_focused_file_cache: dict[str, str] = {}

# Project file index — file name (lowercase) → file_id, for name-based lookups
_project_file_index: dict[str, str] = {}


def set_project_file_index(files: list[dict]) -> None:
    """Populate the file name→id index from a Drive file listing.

    Called by drive_sync after loading a project folder.
    Each file dict should have 'id' and 'name' keys.
    """
    global _project_file_index
    _project_file_index.clear()
    for f in files:
        fid = f.get("id")
        name = f.get("name", "")
        if fid and name:
            # Index both the full name and just the filename (strip path)
            full = name.lower()
            basename = name.rsplit("/", 1)[-1].lower() if "/" in name else full
            _project_file_index[full] = fid
            if basename != full:
                _project_file_index[basename] = fid


def set_project_context(
    doc: PaperDocument,
    comments: list[ReviewerComment],
    images: dict[str, bytes] = None,
    source: str = "",
) -> None:
    """Set the current project context for chat sessions."""
    global _current_doc, _current_comments, _image_cache, _current_doc_source
    _current_doc = doc
    _current_comments = comments
    _current_doc_source = source
    if images:
        _image_cache = images
    logger.info(
        "Project context set: %d sections, %d comments, %d images (source=%s)",
        len(doc.sections),
        len(comments),
        len(_image_cache),
        source,
    )


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are scikick — an AI research companion helping a scientist with their academic work. You can assist with brainstorming, scientific writing, manuscript revision, peer review responses, data analysis, and general research discussion.

## Your Identity
You are powered by an LLM that the user configured in the ⚙ Settings panel. At the bottom of this system prompt you'll find the exact provider and model name you're running on. If the user asks what model or AI you are, answer with that specific provider and model — don't guess or say you don't know.

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

## About This App — scikick
You are the chat interface of a desktop application called **scikick**. Understanding how the app works helps you give accurate answers about its capabilities.

**The app consists of three parts:**
1. **Local server** — runs on the researcher's computer (localhost:8742), handles Google Drive access, file processing, and memory persistence
2. **Chrome extension** — the side panel the researcher is chatting with you through
3. **LLM backend** — that's you, providing the intelligence via API

**What the app does automatically (the researcher doesn't need to ask for this):**
- **Memory is saved after every exchange** — the server writes a `.scikick_memory.json` file to the researcher's Google Drive folder after each of your responses. This file contains the full chat history, reviewer comment statuses, and decisions
- **Cross-computer resume** — if the researcher opens the app on another computer with the same Drive folder, the server downloads the memory file and restores all context. The researcher picks up exactly where they left off
- **Manuscript stays loaded** — once the researcher clicks "Load Project," the server downloads their manuscript and comments from Drive and keeps them in context for the entire session (no need to re-paste)

**What the researcher controls:**
- **⚙ Settings panel** — they can switch LLM providers and models at any time from the gear icon in the extension
- **Which Drive folder** — they paste the Google Drive folder ID to connect their files
- **When to load/reload** — clicking "Load Project" downloads the latest files from Drive

**If the researcher asks about these features:**
- "Can you save this?" / "Do you remember this?" → Explain that the app automatically saves every conversation to their Drive folder as `.scikick_memory.json`, so all progress is persisted
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

def _estimate_context_usage() -> dict:
    """Estimate current context window usage (reused by system prompt and endpoint)."""
    window_size, model = _get_context_window_size()

    system_tokens = _estimate_tokens(SYSTEM_PROMPT)
    resume_tokens = _estimate_tokens(RESUME_PROMPT_EXTENSION)

    retrieval_tokens = (3 * 4000 // 4) + (5 * 500 // 4)  # ~3625 tokens

    history_tokens = 0
    memory = get_current_memory()
    if memory and memory.chat_history:
        history_tokens = sum(
            _estimate_tokens(t.content) for t in memory.chat_history
        )

    scraped_tokens = sum(
        _estimate_tokens(doc.full_text[:6000]) + 200
        for doc in _scraped_docs
    )

    message_reserve = 8000

    total_used = (
        system_tokens + resume_tokens + retrieval_tokens
        + history_tokens + scraped_tokens + message_reserve
    )

    pct_used = round(min((total_used / window_size) * 100, 100), 1)
    remaining = max(window_size - total_used, 0)

    return {
        "model": model,
        "window_size": window_size,
        "total_used": total_used,
        "remaining": remaining,
        "pct_used": pct_used,
    }


def _build_system_prompt() -> str:
    """Build the full system prompt including resume context if available."""
    prompt = SYSTEM_PROMPT

    # Tell the model exactly which provider/model it's running on so it can
    # answer "What model are you?" directly instead of guessing.
    try:
        cfg = get_llm_config()
        prompt += f"\n\n## Your Current Configuration\nYou are running on **{cfg['provider']}** — model: **{cfg['model']}**. The user selected this in the settings panel. If they ask what model you are, tell them this directly."
    except Exception:
        pass

    # Context window awareness — let the model know how much room it has
    try:
        ctx = _estimate_context_usage()
        guidance = ""
        if ctx["pct_used"] > 90:
            guidance = " The window is almost full — be extremely concise (a few sentences at most)."
        elif ctx["pct_used"] > 75:
            guidance = " The window is getting full — keep your responses focused and avoid unnecessary detail."
        elif ctx["pct_used"] > 50:
            guidance = " You have moderate headroom — you can respond at normal length."
        else:
            guidance = " You have plenty of room — feel free to be thorough and expansive."
        prompt += (
            f"\n\n## Context Window Status\n"
            f"Window: {ctx['window_size']:,} tokens | "
            f"In use: ~{ctx['total_used']:,} tokens ({ctx['pct_used']}%) | "
            f"Remaining: ~{ctx['remaining']:,} tokens.{guidance}"
        )
    except Exception:
        pass

    memory = get_current_memory()
    if memory and memory.chat_history:
        prompt += "\n\n" + RESUME_PROMPT_EXTENSION
        prompt += "\n" + build_resume_context()

    return prompt


# Trigger words/phrases that signal the user wants full file content.
# "scan" alone is a trigger — covers "scan this file", "scan Reviewer file", etc.
_FOCUS_TRIGGERS = [
    "scan",           # "scan this file", "can you scan Reviewer comment file?"
    "read this file", "read the file", "read the contents",
    "analyze this file", "analyze the file",
    "look at this file", "look at the file",
    "examine this file", "examine the file",
    "what's in this file", "what is in this file",
    "show me this file", "show me the file",
    "parse this file", "parse the file",
    "tell me about this file", "tell me about the file",
    "check this file", "check the file",
    "review this file", "review the file",
    "go through this file", "go through the file",
    "open this file", "open the file",
]


async def _download_and_parse_file(file_id: str, file_name: str) -> str | None:
    """Download and parse a project file from Drive. Returns parsed text or None."""
    global _focused_file_cache

    # Return cached content if available
    if file_id in _focused_file_cache:
        logger.info("Focus file: using cached content for '%s' (%s)", file_name, file_id)
        return _focused_file_cache[file_id]

    try:
        from drive_sync import download_file

        logger.info("Focus file: downloading '%s' (%s)", file_name, file_id)
        downloaded = await download_file(file_id)
        mime = downloaded.get("mimeType", "")
        parsed_text = ""

        if mime == "application/pdf" and "content_bytes" in downloaded:
            from context_engine import parse_pdf
            content_bytes = bytes.fromhex(downloaded["content_bytes"])
            doc = parse_pdf(content_bytes, file_name)
            parsed_text = doc.full_text
        elif mime in ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",) and "content_bytes" in downloaded:
            from context_engine import parse_docx
            content_bytes = bytes.fromhex(downloaded["content_bytes"])
            doc = parse_docx(content_bytes, file_name)
            parsed_text = doc.full_text
        elif mime in ("application/vnd.google-apps.document", "text/markdown") and "text" in downloaded:
            parsed_text = downloaded["text"]
        elif mime == "application/vnd.google-apps.spreadsheet" and "sheets" in downloaded:
            # Format sheets data as text
            lines = []
            for sheet_title, rows in downloaded.get("sheets", {}).items():
                lines.append(f"\n### Sheet: {sheet_title}")
                for row in rows:
                    lines.append(" | ".join(str(cell) for cell in row))
            parsed_text = "\n".join(lines)
        elif "text" in downloaded:
            parsed_text = downloaded["text"]
        elif "content_bytes" in downloaded:
            from context_engine import parse_text
            raw = bytes.fromhex(downloaded["content_bytes"]).decode("utf-8", errors="replace")
            doc = parse_text(raw, file_name)
            parsed_text = doc.full_text
        else:
            logger.warning("Focus file: unknown format for '%s' (%s)", file_name, mime)
            return None

        # Cache for subsequent messages
        if parsed_text:
            _focused_file_cache[file_id] = parsed_text
            logger.info("Focus file: parsed and cached '%s' (%d chars)", file_name, len(parsed_text))
        return parsed_text

    except Exception as exc:
        logger.error("Focus file: failed to parse '%s': %s", file_name, exc)
        return None


def _should_focus_file(message: str, current_file: dict | None) -> tuple[str | None, str | None]:
    """Check whether the user is asking to focus on a file. Returns (file_id, file_name) or (None, None).

    Two ways to trigger:
    1. current_file is set (file open in browser tab) + trigger phrase in message
    2. User mentions a project file by name in the message + trigger phrase
    """
    msg_lower = message.lower()
    has_trigger = any(trigger in msg_lower for trigger in _FOCUS_TRIGGERS)
    if not has_trigger:
        return None, None

    # Case 1: current_file is set (file open in browser tab)
    if current_file and current_file.get("id"):
        return current_file["id"], current_file.get("name", "unknown")

    # Case 2: user mentioned a project file by name
    global _project_file_index
    for fname_lower, fid in _project_file_index.items():
        # Check if the file name (or its basename without extension) appears in the message
        basename = fname_lower.rsplit(".", 1)[0] if "." in fname_lower else fname_lower
        if fname_lower in msg_lower or basename in msg_lower:
            logger.info("Focus file: matched '%s' in message, file_id=%s", fname_lower, fid)
            # Find the original cased name
            return fid, fname_lower

    return None, None


def _build_user_message(
    message: str,
    include_paper: bool = True,
    include_comments: bool = True,
    focus_figure: Optional[str] = None,
    current_file: Optional[dict] = None,
    session_focus: Optional[str] = None,
    focused_file_content: Optional[str] = None,
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

    # Focused file — full content injected when user asks to scan/read a file
    if focused_file_content and current_file and current_file.get("name"):
        parts.append(
            f"## Focused File: {current_file['name']}\n"
            f"The user has asked to focus on this file. Below is the FULL content. "
            f"Read it carefully and be prepared to answer detailed questions about it.\n\n"
            f"Note: Figures, images, and embedded graphics may appear as garbled binary or "
            f"base64 text — you cannot parse those. Skip over them and focus on the "
            f"readable text content. If the user asks about a figure you cannot read, "
            f"tell them honestly and ask them to describe or paste the figure content.\n"
        )
        parts.append(focused_file_content)
        parts.append("---\n")
    elif current_file and current_file.get("name"):
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

    # Web-scraped papers (accumulate separately from Drive context)
    global _scraped_docs, _scraped_sources
    if _scraped_docs:
        parts.append("## Web-Scraped Papers\n")
        parts.append(f"The user has scraped {len(_scraped_docs)} paper(s) from the web. These are separate from any Drive-loaded project.\n\n")
        for i, sdoc in enumerate(_scraped_docs):
            parts.append(f"### Scraped Paper {i + 1}: {sdoc.title}\n")
            parts.append(f"Source: {_scraped_sources[i] if i < len(_scraped_sources) else 'unknown'}\n")
            if sdoc.abstract:
                parts.append(f"Abstract: {sdoc.abstract[:1500]}\n")
            parts.append(f"Sections: {', '.join(s.heading for s in sdoc.sections)}\n")
            # Include body text (capped to keep context manageable)
            body = sdoc.full_text
            if len(body) > 6000:
                body = body[:6000] + "\n[... truncated]"
            parts.append(f"\n{body}\n")
            parts.append("---\n")

    # The user's actual message
    parts.append(f"## User Message\n{message}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Provider-specific error enrichment
# ---------------------------------------------------------------------------

# Known valid models per provider — used to give helpful suggestions on 404 / invalid-model errors.
_PROVIDER_MODELS: dict[str, str] = {
    "anthropic":  "claude-sonnet-4-6, claude-opus-4-8, claude-haiku-4-5",
    "deepseek":   "deepseek-v4-pro, deepseek-v4-flash",
    "glm":        "glm-4-plus, glm-4-flash, glm-4-long, glm-4-air",
    "openai":     "gpt-4o, gpt-4-turbo, gpt-3.5-turbo",
}


def _clean_error_message(raw: str) -> str:
    """Extract a human-readable message from a raw SDK/API error string.

    Strips JSON blobs and HTTP status prefixes, leaving just the useful text.
    """
    import re as _re

    # Try to pull out a "message" field from embedded JSON
    for pattern in (r"\"message\"\s*:\s*\"([^\"]+)\"", r"'message'\s*:\s*'([^']+)'"):
        m = _re.search(pattern, raw)
        if m:
            return m.group(1)

    # Strip "Error code: NNN - " prefix added by the OpenAI SDK
    cleaned = _re.sub(r"^Error code:\s*\d+\s*[-–—]\s*", "", raw).strip()

    # If it still looks like a raw JSON/dict repr, fall back to a generic message
    if cleaned.startswith("{") or cleaned.startswith("{"):
        return "The API returned an error. See details above."

    return cleaned


def _enrich_error(error_message: str, provider: str, model: str) -> str:
    """Append helpful guidance to raw API errors so the user knows how to fix them."""
    clean = _clean_error_message(error_message)
    parts = [clean]

    msg_lower = error_message.lower()
    is_auth = any(kw in msg_lower for kw in ("401", "unauthorized", "authentication", "x-api-key", "令牌", "过期", "验证"))
    is_model = any(kw in msg_lower for kw in ("model", "invalid_request_error", "not found", "404"))

    # Anthropic returns auth errors for invalid model names too, so always
    # show model suggestions alongside auth guidance for Anthropic.
    if provider == "anthropic" and is_auth:
        is_model = True

    if is_model:
        known = _PROVIDER_MODELS.get(provider)
        if known:
            parts.append(
                f"\n\n💡 Valid models for **{provider}**: {known}. "
                f"You passed: `{model}`. Check the model name in the ⚙ Settings panel."
            )

    if is_auth:
        provider_key_names = {
            "anthropic": "ANTHROPIC_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "glm": "GLM_API_KEY",
            "openai": "OPENAI_API_KEY",
        }
        key_name = provider_key_names.get(provider, "LLM_API_KEY")
        parts.append(
            f"\n\n🔐 Authentication failed for **{provider}**. "
            f"Make sure you're using your **{provider}** API key "
            f"({key_name}) — not a key from another provider. "
            f"Check the ⚙ Settings panel and re-enter the correct key."
        )

    return "".join(parts)


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
        enriched = _enrich_error(str(exc), "anthropic", model)
        logger.error("Anthropic API error: %s", exc)
        yield f"data: {json.dumps({'type': 'error', 'content': enriched})}\n\n"


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
        # Determine provider for error enrichment
        try:
            cfg = get_llm_config()
            provider = cfg.get("provider", "unknown")
            current_model = cfg.get("model", model)
        except Exception:
            provider = "unknown"
            current_model = model
        enriched = _enrich_error(str(exc), provider, current_model)
        logger.error("LLM API error (%s): %s", current_model, exc)
        yield f"data: {json.dumps({'type': 'error', 'content': enriched})}\n\n"


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

    # If the user mentions a trigger phrase while viewing a file or naming a file,
    # download and parse the full content
    focused_file_content: str | None = None
    focused_file_name: str | None = None
    focus_id, focus_name = _should_focus_file(req.message, req.current_file)
    if focus_id:
        focused_file_name = focus_name
        focused_file_content = await _download_and_parse_file(focus_id, focus_name)

    system_prompt = _build_system_prompt()
    user_message = _build_user_message(
        message=req.message,
        include_paper=req.include_paper_context,
        include_comments=req.include_reviewer_comments,
        focus_figure=req.focus_figure,
        current_file={"name": focused_file_name, "id": focus_id} if focused_file_name else req.current_file,
        session_focus=req.session_focus,
        focused_file_content=focused_file_content,
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

    # If the user mentions a trigger phrase while viewing a file or naming a file,
    # download and parse the full content
    focused_file_content: str | None = None
    focused_file_name: str | None = None
    focus_id, focus_name = _should_focus_file(req.message, req.current_file)
    if focus_id:
        focused_file_name = focus_name
        focused_file_content = await _download_and_parse_file(focus_id, focus_name)

    system_prompt = _build_system_prompt()
    user_message = _build_user_message(
        message=req.message,
        include_paper=req.include_paper_context,
        include_comments=req.include_reviewer_comments,
        focus_figure=req.focus_figure,
        current_file={"name": focused_file_name, "id": focus_id} if focused_file_name else req.current_file,
        session_focus=req.session_focus,
        focused_file_content=focused_file_content,
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
        enriched = _enrich_error(str(exc), provider["provider"], provider["model"])
        logger.error("LLM API error: %s", exc)
        raise HTTPException(status_code=502, detail=enriched)

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
                "id": "glm",
                "name": "Zhipu AI (GLM)",
                "sdk": "OpenAI-compatible",
                "models": "glm-4-plus, glm-4-flash, glm-4-long, glm-4-air",
                "env_vars": "LLM_API_KEY or GLM_API_KEY",
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
    "deepseek-v4-pro": 1048576,
    "deepseek-v4-flash": 1048576,
    "deepseek-chat": 131072,
    "deepseek-reasoner": 65536,
    "glm-4-plus": 131072,
    "glm-4-flash": 131072,
    "glm-4-long": 1048576,
    "glm-4-air": 131072,
    "claude-sonnet-4-6": 200000,
    "claude-opus-4-8": 200000,
    "claude-haiku-4-5": 200000,
    "claude-fable-5": 200000,
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
    ctx = _estimate_context_usage()

    # Recompute individual breakdown components for the frontend
    system_tokens = _estimate_tokens(SYSTEM_PROMPT)
    resume_tokens = _estimate_tokens(RESUME_PROMPT_EXTENSION)
    retrieval_chunks_estimate = 3 * 4000 // 4
    retrieval_comments_estimate = 5 * 500 // 4

    history_tokens = 0
    memory = get_current_memory()
    if memory and memory.chat_history:
        history_tokens = sum(
            _estimate_tokens(t.content) for t in memory.chat_history
        )

    scraped_tokens = sum(
        _estimate_tokens(doc.full_text[:6000]) + 200
        for doc in _scraped_docs
    )

    pct_used = ctx["pct_used"]
    remaining = ctx["remaining"]

    return {
        "model": ctx["model"],
        "window_size": ctx["window_size"],
        "breakdown": {
            "system_prompt": system_tokens,
            "resume_prompt": resume_tokens,
            "retrieval_chunks": retrieval_chunks_estimate,
            "retrieval_comments": retrieval_comments_estimate,
            "chat_history": history_tokens,
            "scraped_papers": scraped_tokens,
            "message_reserve": 8000,
        },
        "total_used": ctx["total_used"],
        "remaining": remaining,
        "pct_used": pct_used,
        "pct_free": round(100 - pct_used, 1),
        "manuscript_available": _current_doc is not None,
        "manuscript_total_chars": len(_current_doc.full_text) if _current_doc else 0,
        "scraped_papers_count": len(_scraped_docs),
        "scraped_total_chars": sum(len(doc.full_text) for doc in _scraped_docs),
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
    """Get a summary of the current project context.

    Always returns whatever is loaded — manuscript, comments, and scraped
    papers are independent and may be present without one another.
    """
    if _current_doc is not None:
        paper = {
            "title": _current_doc.title,
            "sections": [s.heading for s in _current_doc.sections],
            "figures": [f.filename for f in _current_doc.figures],
            "full_text_length": len(_current_doc.full_text),
        }
        comments = [
            {
                "id": c.id,
                "reviewer": c.reviewer,
                "severity": c.severity,
                "text_preview": c.text[:200],
                "related_sections": c.related_sections,
                "related_figures": c.related_figures,
            }
            for c in _current_comments
        ]
        images = list(_image_cache.keys())
        loaded = True
    else:
        paper = None
        comments = []
        images = []
        loaded = False

    scraped_papers = [
        {
            "title": doc.title,
            "url": _scraped_sources[i] if i < len(_scraped_sources) else "",
            "sections": [s.heading for s in doc.sections],
            "full_text_length": len(doc.full_text),
        }
        for i, doc in enumerate(_scraped_docs)
    ]

    return {
        "loaded": loaded,
        "paper": paper,
        "comments": comments,
        "images": images,
        "scraped_papers": scraped_papers,
    }


# ---------------------------------------------------------------------------
# Scraped papers management
# ---------------------------------------------------------------------------


@router.get("/scraped")
async def list_scraped():
    """List all web-scraped papers currently in context."""
    return {
        "papers": [
            {
                "index": i,
                "title": doc.title,
                "url": _scraped_sources[i] if i < len(_scraped_sources) else "",
                "sections": [s.heading for s in doc.sections],
                "full_text_length": len(doc.full_text),
            }
            for i, doc in enumerate(_scraped_docs)
        ],
        "count": len(_scraped_docs),
    }


@router.delete("/scraped")
async def clear_scraped(index: int = None):
    """Clear scraped papers. Pass ?index=N to remove one, or omit to clear all."""
    global _scraped_docs, _scraped_sources
    if index is not None:
        if 0 <= index < len(_scraped_docs):
            removed = _scraped_docs.pop(index)
            _scraped_sources.pop(index)
            return {"status": "removed", "title": removed.title, "remaining": len(_scraped_docs)}
        raise HTTPException(status_code=404, detail=f"No scraped paper at index {index}")
    count = len(_scraped_docs)
    _scraped_docs = []
    _scraped_sources = []
    return {"status": "cleared", "removed": count}


@router.post("/reset")
async def reset_all_state():
    """Wipe all in-memory state — manuscript, comments, scraped papers, memory.

    Returns the server to a clean slate without restarting.  Use this to
    start a fresh session without losing the LLM configuration.
    """
    global _current_doc, _current_comments, _image_cache, _current_doc_source
    global _scraped_docs, _scraped_sources
    global _focused_file_cache
    global _project_file_index

    from memory_manager import get_current_memory, set_current_memory

    # Track what we're clearing for the response
    existing_memory = get_current_memory()
    had_paper = _current_doc is not None
    had_comments = len(_current_comments)
    had_scraped = len(_scraped_docs)
    had_memory = existing_memory is not None

    _current_doc = None
    _current_comments = []
    _image_cache = {}
    _current_doc_source = ""
    _scraped_docs = []
    _scraped_sources = []
    _focused_file_cache = {}
    _project_file_index = {}

    if existing_memory is not None:
        set_current_memory(None)

    return {
        "status": "reset",
        "cleared": {
            "manuscript": had_paper,
            "comments": had_comments,
            "scraped_papers": had_scraped,
            "memory": had_memory,
        },
    }


@router.post("/unload-project")
async def unload_project():
    """Clear the loaded Drive project (manuscript, comments, memory)
    while keeping scraped articles intact.

    Use this when switching projects without losing web-scraped papers.
    """
    global _current_doc, _current_comments, _image_cache, _current_doc_source
    global _focused_file_cache
    global _project_file_index

    from memory_manager import get_current_memory, set_current_memory

    existing_memory = get_current_memory()
    had_paper = _current_doc is not None
    had_comments = len(_current_comments)
    had_memory = existing_memory is not None

    _current_doc = None
    _current_comments = []
    _image_cache = {}
    _focused_file_cache = {}
    _current_doc_source = ""
    _project_file_index = {}

    if existing_memory is not None:
        set_current_memory(None)

    return {
        "status": "unloaded",
        "cleared": {
            "manuscript": had_paper,
            "comments": had_comments,
            "memory": had_memory,
        },
        "scraped_preserved": len(_scraped_docs),
    }


# ---------------------------------------------------------------------------
# Web scraping — load paper from a journal webpage
# ---------------------------------------------------------------------------


class ScrapeRequest(BaseModel):
    url: str
    html: str = ""  # page HTML extracted by the extension from the active tab


@router.post("/scrape")
async def scrape_webpage(req: ScrapeRequest):
    """Scrape a paper from a journal webpage and load it as chat context.

    The Chrome extension extracts the full page HTML from the active browser
    tab via chrome.scripting.executeScript — this uses the user's authenticated
    session so journal sites with institutional access work.
    """
    from scraper import extract_content

    if not req.html or len(req.html) < 100:
        raise HTTPException(
            status_code=400,
            detail="No page HTML provided. The extension must extract the page content first.",
        )

    try:
        logger.info("Scrape: parsing %d chars of HTML for %s", len(req.html), req.url)
        doc = extract_content(req.html, req.url)
    except Exception as exc:
        logger.error("Scrape HTTP error for %s: %s", req.url, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Could not fetch the page: {exc}",
        )
    except Exception as exc:
        logger.error("Scrape error for %s: %s", req.url, exc)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to scrape page: {exc}",
        )

    # Add to scraped papers (accumulate — multiple papers can coexist).
    # This is separate from the Drive-loaded project context.
    global _scraped_docs, _scraped_sources
    _scraped_docs.append(doc)
    _scraped_sources.append(req.url)

    return {
        "status": "scraped",
        "url": req.url,
        "title": doc.title,
        "abstract_length": len(doc.abstract),
        "full_text_length": len(doc.full_text),
        "sections": [s.heading for s in doc.sections],
        "section_count": len(doc.sections),
        "scraped_count": len(_scraped_docs),
    }
