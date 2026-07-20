"""Chat handler — multi-provider LLM integration with streaming, context injection.

Supported providers:
  - anthropic (Anthropic SDK)
  - deepseek, glm, openai, gemini, kimi, custom (OpenAI-compatible SDK)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import get_llm_config, _is_local_provider
from context_engine import (
    PaperDocument,
    ReviewerComment,
    best_chunk_score,
    retrieve_context,
)
from memory_manager import (
    GoalState,
    build_resume_context,
    goal_block,
    goal_recap_text,
    get_current_memory,
    update_memory_after_chat,
    _save_local,
)

logger = logging.getLogger("paper-assistant.chat")
router = APIRouter()

# ---------------------------------------------------------------------------
# Global state for the current project
# ---------------------------------------------------------------------------

# Coarse lock guarding mutations of the project-state globals below
# (reset / unload / load-project reassignments). One lock for the whole bundle
# — coarse by design to avoid deadlocks. Chat send paths don't mutate these
# (they only read), so they don't take it; their memory write goes through
# memory_manager._memory_lock instead.
_state_lock = asyncio.Lock()

_current_doc: Optional[PaperDocument] = None
_current_comments: list[ReviewerComment] = []
_image_cache: dict[str, bytes] = {}  # filename -> raw bytes
_current_doc_source: str = ""  # "drive:<folder_id>"
# Drive file id of the loaded manuscript, so a scan/focus request for that
# same file can short-circuit to the already-parsed _current_doc instead of
# re-downloading it from Drive (slow, and can time out on large files).
_current_doc_file_id: str = ""
# Drive file NAME of the loaded manuscript (e.g. "01_..._Main_Submission.docx"),
# so confirmations can list it as an in-context file. The manuscript is NOT in
# _loaded_docs (it's auto-loaded as _current_doc every turn), but the user
# still expects to see it when they ask "what's in context".
_current_doc_file_name: str = ""

# Web-scraped papers — accumulate (multiple allowed), separate from Drive context
_scraped_docs: list[PaperDocument] = []
_scraped_sources: list[str] = []  # URLs, parallel to _scraped_docs

# Focused file cache — file_id → parsed text content
_focused_file_cache: dict[str, str] = {}

# Project file index — file name (lowercase) → list of {id, name} entries,
# for name-based lookups. A list (not a single id) so that duplicate basenames
# in different subfolders (e.g. subA/notes.txt, subB/notes.txt) don't
# overwrite each other; the consumer disambiguates.
_project_file_index: dict[str, list[dict]] = {}

# Classified project structure — a list of {file_id, name, type, size, mime}
# for EVERY file in the loaded Drive folder, where type is one of
# 'manuscript' | 'supplement' | 'reviewer_comment' | 'supporting' |
# 'miscellaneous'. Built from the file listing (names + the detected
# manuscript id). 'supporting' files are also parsed into _project_docs
# (below) so their chunks are searchable each turn; 'miscellaneous' files
# (images, archives, binaries) are listed but not parsed.
_project_structure: list[dict] = []

# Parsed non-manuscript project files: list of
# {"file_id", "name", "type", "doc": PaperDocument}. Populated on Load
# Project by drive_sync for every text-bearing supporting/supplement file.
# retrieve_context searches these each turn with a type-weighted chunk budget
# (manuscript + reviewer comments get the most). The manuscript itself stays
# in _current_doc; reviewer comments stay in _current_comments. Cleared on
# /reset and /unload-project.
_project_docs: list[dict] = []

# One/two-line project summary inferred at Load Project from the manuscript
# title + sampled chunks (best-effort LLM call; falls back to the title).
# Shown in the side panel's load message and injected into the system prompt
# so the model knows the project at a glance. Regenerated each load.
_project_summary: str = ""

# Set True after the server injects a "which document do you want me to scan?"
# clarification. The next user message is then treated as the document choice
# (even without a scan trigger word) and resolved by _resolve_doc_choice.
# Session-scoped — cleared on choice resolution, /reset, /unload-project.
_awaiting_doc_choice: bool = False

# Proactive scan offer: when a question seems to need the full document but
# the user didn't explicitly say "scan", we offer once ("want me to scan this
# document really quickly?"). _awaiting_scan_confirmation is set while we wait
# for the yes/no reply; _scan_preference remembers the answer so we don't ask
# again that session ("yes" → auto-scan future implicit-need questions,
# "no" → just use targeted chunks). Session-scoped; cleared on /reset,
# /unload-project.
_awaiting_scan_confirmation: bool = False
_scan_preference: str = ""  # "" | "yes" | "no"

# --- Project goal onboarding -------------------------------------------------
# The user's chosen goal/mode for the loaded project is persisted in
# memory.goal (GoalState). The first time a project is loaded with no goal —
# or after a "change goal" command — this state machine walks the user through
# mode-specific questions, capturing free-text answers here before finalizing
# them into memory.goal. Mode + subtype are button-chosen via POST /chat/goal;
# the remaining free-text field is asked as an assistant message and the
# user's reply is captured here (bypassing the LLM), the same intercept
# pattern used for _awaiting_doc_choice / _awaiting_scan_confirmation.
_goal_onboarding: bool = False  # pipeline active (between mode pick and finalize)
_awaiting_goal_field: str = ""  # journal | grant_details | target | freeform
_pending_goal: Optional[GoalState] = None  # accumulates answers until finalize
_goal_discussing: bool = False  # a goal-field reply is being discussed, not yet answered

# Phrases that mark a goal-field reply as a discussion/uncertainty reply rather
# than a real answer — such replies are passed to the LLM for a guided chat
# instead of being captured verbatim and looked up. Conservative: only clearly
# discussion-shaped replies match; real answers ("UCLA", "Nature", "R01") do not.
_DISCUSSION_REPLY_MARKERS = (
    # uncertainty
    "not sure", "unsure", "don't know", "dont know", "no idea",
    "haven't decided", "havent decided", "still deciding", "thinking about",
    "debating", "torn", "weighing", "undecided", "not certain",
    # discussion request
    "could we discuss", "can we discuss", "let's discuss", "lets discuss",
    "can we talk", "could we talk", "talk it through", "what do you think",
    "what would you recommend", "any suggestions", "any advice",
    "help me decide", "help me choose", "which should i",
)


def _is_discussion_reply(message: str) -> bool:
    """True if a goal-onboarding reply is a discussion/uncertainty reply (e.g.
    "I'm not sure, could we discuss this?") rather than a concrete answer."""
    low = (message or "").strip().lower()
    if not low:
        return False
    return any(marker in low for marker in _DISCUSSION_REPLY_MARKERS)


# Phrases that re-trigger the goal pipeline (clears the saved goal first).
_CHANGE_GOAL_PHRASES = (
    "change goal", "change the goal", "new goal", "switch goal",
    "change mode", "switch mode", "reset goal",
)

# Loaded documents — project files the user explicitly asked to "keep" in
# context across turns (e.g. "scan and keep the supplement"). Each entry is
# {"file_id", "name", "text"}. Persists for the session (like _scraped_docs),
# injected every turn, removable on request. Distinct from the one-shot
# "Focused File" block (single turn) and from _current_doc (the single
# designated manuscript). Cleared on /reset and /unload-project.
_loaded_docs: list[dict] = []

# Per-request keep acknowledgment, set in _prepare_scan_context (via
# _add_loaded_doc) and consumed+cleared in _build_user_message. Always
# {"kind": "kept", "name": ...} — whether a non-manuscript file was just kept
# OR the manuscript was promoted from chunked _current_doc to full-text kept.
# Triggers a confirmation listing every file in context so the model neither
# confabulates a success nor under-reports the kept set.
_keep_ack: Optional[dict] = None

# Per-turn scan injection (one-shot focused file + full-manuscript deep scan)
# from the most recent turn. One-shot scans aren't persistent state, so
# _loaded_docs/scraped/history don't capture them — without this the
# context-usage meter wouldn't move when the user scans a file. Counted by
# the meter, reset to 0 on turns with no scan, cleared on /reset +
# /unload-project.
_last_turn_scan_chars: int = 0

# Actual token count of the most recent LLM request (system_prompt +
# user_message), recorded after each /send. The panel's context meter shows
# this so it reflects the REAL prompt size for the next/last request —
# one-shot scans raise it (their tokens are part of that request) and it drops
# again when a smaller request is sent. This mirrors Claude Code's meter: if
# tokens are sent to the model, they count. 0 until the first request, at
# which point the meter falls back to a standing projection.
_last_request_tokens: int = 0
_last_request_system_tokens: int = 0
_last_request_user_tokens: int = 0

# Phrases that signal "add this file to the persistent loaded set" (not just a
# one-shot scan). Phrase-based, not bare words, so "keep it concise" / "add
# this to the results" don't false-trigger.
_KEEP_MARKERS = (
    "keep this loaded", "keep that loaded", "keep it loaded",
    "keep this document", "keep this file", "keep that document", "keep that file",
    "keep the document", "keep the file",
    "scan and keep", "read and keep", "load and keep",
    "also load", "load too",
    "add to context", "add to the context",
    "remember this file", "remember this document",
    "keep in context", "keep on hand",
    "persist this",
)
# Phrases that signal "drop a file from the persistent loaded set". Removal
# needs a verb (remove/unload/drop/forget/stop loading) AND a document
# reference — either a named project file or "this/that/the document/file" —
# so a normal editing request like "remove the comma after haplotig" can't
# silently drop the open tab from the loaded set.
_REMOVE_VERBS = (
    "remove", "unload", "drop", "forget",
    "stop loading", "stop keeping", "stop including",
)
_REMOVE_DOC_REFS = (
    "this document", "this file", "that document", "that file",
    "the document", "the file", "these documents", "those files",
)
_REMOVE_ALL_MARKERS = (
    "clear all loaded", "remove all loaded", "unload all loaded",
    "clear loaded documents", "clear loaded files",
)


def set_project_file_index(files: list[dict], manuscript_file_id: str = "") -> None:
    """Populate the file name→id index and the classified structure summary.

    Called by drive_sync after loading a project folder.
    Each file dict should have 'id', 'name', 'mimeType', and 'size' keys.
    ``manuscript_file_id`` is the Drive id of the file _find_manuscript picked,
    so it can be labelled 'manuscript' in the structure summary.
    """
    global _project_file_index, _project_structure
    _project_file_index.clear()
    _project_structure.clear()
    # Lazy import avoids a circular import (drive_sync imports this module).
    from drive_sync import classify_project_file

    type_counts: dict[str, int] = {}
    for f in files:
        fid = f.get("id")
        name = f.get("name", "")
        mime = f.get("mimeType", "")
        size = f.get("size", 0)
        if fid and name:
            entry = {"id": fid, "name": name}
            # Index both the full name and just the filename (strip path)
            full = name.lower()
            basename = name.rsplit("/", 1)[-1].lower() if "/" in name else full
            _project_file_index.setdefault(full, []).append(entry)
            if basename != full:
                _project_file_index.setdefault(basename, []).append(entry)
        # Classified structure entry for every file (even non-parseable ones,
        # so the model sees the full folder layout).
        ftype = classify_project_file(name, mime, manuscript_file_id, fid or "")
        type_counts[ftype] = type_counts.get(ftype, 0) + 1
        _project_structure.append({
            "file_id": fid or "",
            "name": name,
            "type": ftype,
            "size": int(size) if size else 0,
            "mime": mime,
        })
    if _project_structure:
        breakdown = ", ".join(f"{c} {t}" for t, c in sorted(type_counts.items()))
        logger.info("Project structure: %d files (%s)", len(_project_structure), breakdown)


def get_project_file_counts() -> dict:
    """Per-category file counts for the loaded project, derived from
    ``_project_structure``. Returned by the load-context endpoint so the
    panel can show an accurate folder breakdown on both fresh loads and
    unchanged re-loads (the structure survives a no-op reload)."""
    counts = {
        "total": len(_project_structure),
        "manuscript": 0,
        "supplement": 0,
        "supporting": 0,
        "reviewer_comments": 0,
        "miscellaneous": 0,
    }
    for f in _project_structure:
        t = f.get("type")
        if t == "reviewer_comment":
            counts["reviewer_comments"] += 1
        elif t in counts:
            counts[t] += 1
    return counts


def set_project_context(
    doc: Optional[PaperDocument],
    comments: list[ReviewerComment],
    images: dict[str, bytes] = None,
    source: str = "",
    doc_file_id: str = "",
    doc_file_name: str = "",
) -> None:
    """Set the current project context for chat sessions."""
    global _current_doc, _current_comments, _image_cache, _current_doc_source
    global _current_doc_file_id, _current_doc_file_name
    _current_doc = doc
    _current_comments = comments
    _current_doc_source = source
    _current_doc_file_id = doc_file_id
    _current_doc_file_name = doc_file_name
    if images:
        _image_cache = images
    logger.info(
        "Project context set: %d sections, %d comments, %d images (source=%s, doc_file_id=%s)",
        len(doc.sections) if doc else 0,
        len(comments),
        len(_image_cache),
        source,
        doc_file_id or "(none)",
    )


def set_project_docs(docs: list[dict]) -> None:
    """Store the parsed non-manuscript project files (supplements/supporting).

    Called by drive_sync after the per-file parse pass on Load Project. Each
    entry is {"file_id", "name", "type", "doc": PaperDocument}. Replaces the
    previous set (a folder switch drops the old project's docs).
    """
    global _project_docs
    _project_docs = list(docs)
    if _project_docs:
        by_type: dict[str, int] = {}
        for d in _project_docs:
            by_type[d["type"]] = by_type.get(d["type"], 0) + 1
        breakdown = ", ".join(f"{c} {t}" for t, c in sorted(by_type.items()))
        logger.info("Project docs: %d parsed (%s)", len(_project_docs), breakdown)


def set_project_summary(summary: str) -> None:
    """Store the load-time project summary (shown in panel + system prompt)."""
    global _project_summary
    _project_summary = summary or ""


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
- **Never fabricate document content.** When you reference text from a loaded/kept document (the "## Loaded Documents" block), the manuscript, reviewer comments, or any scraped article, it must come from text actually present there — quote it or paraphrase it closely. Do NOT invent figure titles, figure legends, panel descriptions, section headings, captions, tables, or data values that you cannot locate in the provided text. If a figure or section exists only as an embedded image, its visual content is NOT available to you — say so, rather than guessing what it depicts.
- **If you cannot find specific content the user asks about** (a figure legend, a section, a value, a caption), say so plainly: "I don't see X in the loaded document." Do NOT claim the content is blank, missing, or "not filled in yet" unless you have scanned the entire provided text and confirmed the absence by quoting what IS there. A long run of blank lines in the extracted text almost always means an embedded image or layout spacing was stripped during extraction — it does NOT mean text is missing. Search the rest of the document (including later sections) before concluding any content is absent.
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
- **Important content is saved, not every word** — after each exchange, the server buffers the conversation locally. Trivial exchanges (acknowledgements like "ok"/"thanks") are ignored. Periodically (about every two minutes, and when the session closes), an LLM digest distils the substantive exchanges into structured memory: decisions made, reviewer-comment status changes, key facts, and a short recap. That structured memory — not the raw back-and-forth — is written to a `.scikick_memory.json` file in the researcher's Google Drive folder. Only a short rolling window of recent messages is kept verbatim for immediate context; older chatter is replaced by the distilled summary.
- **Cross-computer resume** — if the researcher opens the app on another computer with the same Drive folder, the server downloads the memory file and restores the structured context (decisions, reviewer-comment states, key facts, recap). They pick up where they left off, though the full word-for-word history of past sessions is not retained — only what was distilled.
- **Manuscript stays loaded** — once the researcher clicks "Load Project," the server downloads their manuscript and comments from Drive and keeps them in context for the entire session (no need to re-paste)

**What the researcher controls:**
- **⚙ Settings panel** — they can switch LLM providers and models at any time from the gear icon in the extension
- **Which Drive folder** — they paste the Google Drive folder ID to connect their files
- **When to load/reload** — clicking "Load Project" downloads the latest files from Drive

**If the researcher asks about these features:**
- "Can you save this?" / "Do you remember this?" → Explain that the app automatically distils important points from the conversation (decisions, reviewer-comment updates, key facts) into a `.scikick_memory.json` file on their Drive folder. Trivial chatter isn't kept, but anything substantive is. Tell them the safe way to make sure something is remembered is to state it as a clear decision or fact.
- "Will this be here if I switch computers?" → Yes — the distilled memory syncs to Google Drive. On any new computer, they clone the repo, run `./start.sh --setup`, and paste the same Drive folder ID. The structured context is restored, though not the verbatim history of prior sessions.
- "How do I change the model?" → They can click the ⚙ gear icon in the extension's top bar to switch providers and models immediately
"""

RESUME_PROMPT_EXTENSION = """
## Session Resumed
The researcher is continuing a previous session. Below is a summary of where they left off.

**Kept/scanned files do not survive a restart.** Project files the researcher asked to "keep in context" or "scan" are held in server memory only and are cleared whenever the server restarts — they are NOT restored from this resume summary. The "## Loaded Documents" block in the current message is the SOLE source of truth for what is loaded right now. The "### Previous Context" recap below may mention files that were scanned or kept in a PRIOR session — those mentions are stale and do NOT mean the files are currently loaded.

If the "## Loaded Documents" block lists no kept files (or says none are kept), then no files are in context, even if the recap below mentions files kept or scanned in a prior session. Do not tell the researcher a file is "already loaded", "already kept", "already scanned", "already in context", "I have it", or "I've read it" unless that file appears in the current "## Loaded Documents" block. (The manuscript and reviewer comments ARE auto-loaded on Load Project, so they may legitimately be in context — but supplements and other project files are not, unless listed.) If the researcher asks to scan or keep a file, act on the request — do not claim it is already done.
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

def _estimate_context_usage(include_transient: bool = False) -> dict:
    """Estimate current context window usage.

    By default reflects STANDING context — persistent items only (kept docs,
    chat history, scraped papers, system/retrieval baseline). This is what the
    panel's % free bar shows, so a one-shot scan (transient — gone next turn)
    doesn't make it spike and drop. Pass ``include_transient=True`` to also
    count the most recent turn's scan injection; the per-turn system-prompt
    guidance uses that so the model knows when a scan made the turn tight.
    """
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

    # Kept documents are injected every turn (capped at the kept-doc budget,
    # which scales with the model's context window), so they count against the
    # per-message budget — not just once.
    _kept_cap = _doc_char_budget(0.25)
    loaded_tokens = sum(
        _estimate_tokens(d["text"][:_kept_cap]) + 100
        for d in _loaded_docs
    )

    # One-shot / deep scans from the most recent turn (transient, not
    # persistent) — only counted for per-turn guidance, not the standing meter.
    last_turn_scan_tokens = (_last_turn_scan_chars // 4) if include_transient else 0

    message_reserve = 8000

    total_used = (
        system_tokens + resume_tokens + retrieval_tokens
        + history_tokens + scraped_tokens + loaded_tokens
        + last_turn_scan_tokens + message_reserve
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

    # Context window awareness — let the model know how much room it has.
    # include_transient=True so the model sees this turn's scan injection
    # (the panel bar deliberately does NOT, to stay stable across turns).
    try:
        ctx = _estimate_context_usage(include_transient=True)
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

    # Succinct project summary inferred at Load Project from the manuscript
    # title + sampled chunks. Gives the model a one-glance sense of what the
    # project is about before any retrieval.
    if _project_summary:
        prompt += f"\n\n## Project Summary\n{_project_summary}\n"

    # The user's persisted project goal/mode (chosen on first load). When set
    # this is the source of truth for the session's purpose and supersedes the
    # per-message "session focus" hint. Built from memory.goal so it reflects
    # the saved goal on every turn (including across restarts).
    memory = get_current_memory()
    if memory and memory.goal and memory.goal.mode:
        prompt += "\n\n" + goal_block(memory.goal)

    # While a goal-field reply is being discussed (the user gave a discussion/
    # uncertainty reply during onboarding instead of a concrete answer), guide
    # the LLM to help them think it through — without claiming anything was set.
    if _goal_onboarding and _awaiting_goal_field and _goal_discussing:
        label = {
            "journal": "target journal",
            "grant_details": "grant details",
            "target": "target school or program",
            "freeform": "application details",
        }.get(_awaiting_goal_field, "goal")
        prompt += (
            "\n\n## Goal Decision In Progress\n"
            f"The user is deciding on their {label} for this project's goal and "
            "wants to discuss it first. Help them think it through conversationally "
            "and briefly — offer a few considerations, maybe one clarifying "
            "question. Do NOT claim the goal has been set or that you recorded "
            "anything. When they name a concrete choice, they'll give it as a "
            "direct answer and it will be captured then."
        )

    # Classified project structure — a metadata overview of every file in the
    # loaded Drive folder (names + types only). Tells the model what's in the
    # project so it can answer "what files are in this project?" and route
    # scan/keep requests accurately. Text-bearing files are also parsed and
    # chunk-searched each turn (see _project_docs); only 'miscellaneous'
    # (binary) files are listed-but-unreadable.
    if _project_structure:
        def _human_size(n: int) -> str:
            if not n:
                return "?"
            if n >= 1_000_000:
                return f"{n / 1_000_000:.1f} MB"
            if n >= 1_000:
                return f"{n / 1_000:.0f} KB"
            return f"{n} B"

        lines = [
            f"- {e['name']} — {e['type']} ({_human_size(e['size'])})"
            for e in _project_structure
        ]
        structure_block = (
            "\n\n## Project Structure\n"
            "Files in this project (classified):\n"
            + "\n".join(lines)
            + "\n"
        )
        if _current_doc is not None and _current_doc.sections:
            headings = ", ".join(s.heading for s in _current_doc.sections if s.heading)
            if headings:
                structure_block += (
                    f"Manuscript section headings: {headings}\n"
                )
        structure_block += (
            "Each turn the top keyword-matching chunks are pulled automatically "
            "from the manuscript and reviewer comments (weighted heaviest), and "
            "from supplements/supporting files (lighter). 'miscellaneous' files "
            "(images, archives) are listed but not text-searchable. The user can "
            "also scan-and-keep any file to load its full text every turn. "
            "If the user asks what's in the project, answer from this list.\n"
        )
        prompt += structure_block

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
            from file_processor import parse_pdf
            content_bytes = bytes.fromhex(downloaded["content_bytes"])
            doc = parse_pdf(content_bytes, file_name)
            parsed_text = doc.full_text
        elif mime in ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",) and "content_bytes" in downloaded:
            from file_processor import parse_docx
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
            from file_processor import parse_text
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


def _filename_tokens(name: str) -> set[str]:
    """Tokens from a filename: lowercase alnum runs of length >= 4.

    Used for fuzzy name matching so "supplemental material" / "reviewer
    comments" can match a file whose real name is underscored. Short runs
    (e.g. "02", "fcs", "br") are dropped — they aren't distinctive.
    """
    return set(re.findall(r"[a-z0-9]{4,}", name.lower()))


def _match_named_file_by_tokens(message: str) -> tuple[str, str] | None:
    """Fallback matcher: find a project file by distinctive filename tokens.

    Lets the user say "scan the supplemental material" or "scan the reviewer
    comments" without typing the literal underscored filename. A token matches
    if the user's word equals it or one is a prefix of the other (so
    "supplement" matches "supplemental", "comments" matches "comment"). Only
    tokens DISTINCTIVE to a single file count — "manuscript" appears in both
    the main and supplement names, so it can't decide a match on its own.
    """
    global _project_file_index
    files: list[dict] = []
    seen_ids: set[str] = set()
    for entries in _project_file_index.values():
        for e in entries:
            fid = e.get("id")
            if fid and fid not in seen_ids:
                seen_ids.add(fid)
                files.append(e)
    if not files:
        return None

    token_file_count: dict[str, int] = {}
    file_tokens: list[tuple[dict, set[str]]] = []
    for f in files:
        toks = _filename_tokens(f.get("name", ""))
        file_tokens.append((f, toks))
        for t in toks:
            token_file_count[t] = token_file_count.get(t, 0) + 1

    msg_tokens = _filename_tokens(message)
    if not msg_tokens:
        return None

    def _tok_match(w: str, t: str) -> bool:
        # exact, or one is a prefix of the other (min 4 chars) — covers
        # "supplement"→"supplemental" and "comments"→"comment".
        if w == t:
            return True
        if len(w) >= 4 and len(t) >= 4:
            return t.startswith(w) or w.startswith(t)
        return False

    best_file: dict | None = None
    best_score = 0
    for f, toks in file_tokens:
        score = 0
        for t in toks:
            if token_file_count.get(t, 0) != 1:
                continue  # not distinctive — appears in multiple filenames
            if any(_tok_match(w, t) for w in msg_tokens):
                score += 1
        if score > best_score:
            best_score = score
            best_file = f

    if best_file is None or best_score < 1:
        return None
    # DEBUG, not INFO: _match_named_file_by_tokens is a pure helper called
    # multiple times per request, so INFO would log the same match repeatedly.
    logger.debug(
        "Focus file: token-matched '%s' (score=%d), file_id=%s",
        best_file.get("name"), best_score, best_file.get("id"),
    )
    return best_file["id"], best_file["name"]


def _match_named_file(message: str) -> tuple[str, str] | None:
    """Return (file_id, file_name) if the message mentions a project file by name.

    Gather every matching key and pick the LONGEST — a full path (e.g.
    "subb/notes.txt") is more specific than a bare basename ("notes.txt") and
    should win, otherwise a duplicate basename in another subfolder could
    shadow the intended file. No trigger-phrase gate: this is pure name
    matching, reused by both the explicit-scan and choice-resolution paths.

    Falls back to distinctive-token matching so natural phrasing ("scan the
    supplemental material") works without typing the literal underscored name.
    """
    msg_lower = message.lower()
    global _project_file_index
    best_key = None
    best_entries = None
    for fname_lower, entries in _project_file_index.items():
        basename = fname_lower.rsplit(".", 1)[0] if "." in fname_lower else fname_lower
        if fname_lower in msg_lower or basename in msg_lower:
            if best_key is None or len(fname_lower) > len(best_key):
                best_key = fname_lower
                best_entries = entries

    if best_entries is not None:
        entry = best_entries[0]
        if len(best_entries) > 1:
            # Duplicate basenames across subfolders — pick the first and warn;
            # the user can disambiguate by naming the full path.
            logger.warning(
                "Focus file: '%s' matches %d files; using the first (id=%s). "
                "Specify the full path to pick a different one.",
                best_key, len(best_entries), entry["id"],
            )
        else:
            # DEBUG, not INFO: _match_named_file is a pure helper reused by
            # several call sites per request (classify + keep/focus handlers),
            # so INFO would log the same match several times.
            logger.debug("Focus file: matched '%s' in message, file_id=%s", best_key, entry["id"])
        return entry["id"], entry["name"]

    # No literal-substring match — try distinctive-token matching.
    return _match_named_file_by_tokens(message)


# Phrases that point at the *currently-viewed* browser-tab file vs. the *loaded
# manuscript*. Substring-matched against the lowercased message. Order matters
# where one could contain another, so check current_refs first in callers.
_CURRENT_FILE_REFS = (
    "this file", "this document", "current document", "current file",
    "the current one", "this one", "the one i'm on", "the one im on",
    "the one i'm viewing", "the one im viewing", "the one i'm looking at",
    "the page i'm on", "the open tab", "the tab",
)
_MANUSCRIPT_REFS = (
    "manuscript", "the paper", "the supplement", "supplemental material",
    "loaded document", "loaded manuscript", "the main text", "main manuscript",
)
# Whole-document / coverage intent — "the answer is probably somewhere in the
# doc and targeted chunks may not have caught it."
_COVERAGE_REFS = (
    "does the document", "does the manuscript", "does the paper",
    "is there", "are there any", "any mention", "any reference",
    "find all", "list all", "scan and check", "check the document",
    "check the manuscript", "check the whole", "throughout the",
    "in the whole", "entire document", "entire manuscript", "whole document",
    "whole manuscript", "anywhere in",
)


def _has_any(text_lower: str, phrases: tuple[str, ...]) -> bool:
    return any(p in text_lower for p in phrases)


def _classify_scan_intent(
    message: str,
    current_file: dict | None,
    doc: Optional[PaperDocument],
    awaiting_choice: bool,
) -> str:
    """Decide how to handle a possible document-scan request.

    Returns one of:
    - "deep_manuscript"  : inject the full loaded manuscript
    - "focus_current"    : download + inject the file open in the browser tab
    - "focus_named"      : download + inject a project file named in the message
    - "keep_named"       : parse + ADD a named file to the persistent loaded set
    - "keep_current"     : parse + ADD the browser-tab file to the loaded set
    - "keep_manuscript"  : keep requested for the manuscript (already in context)
    - "remove_named"     : drop a named file from the loaded set
    - "remove_current"   : drop the browser-tab file from the loaded set
    - "remove_all"       : clear the entire loaded set
    - "ask"              : ambiguous — ask the user which document to scan
    - "offer_scan"       : implicit need, single manuscript — offer a quick scan
    - "targeted"         : no full scan; use normal top-k chunk retrieval

    Only asks when there is a genuine choice (a Drive file open in the tab AND
    a manuscript/other files available) and the user didn't already specify a
    target. With a single candidate, it scans directly; with no scan intent,
    it falls through to targeted retrieval.
    """
    msg_lower = message.lower()

    has_current_file = bool(current_file and current_file.get("id"))
    has_manuscript = doc is not None
    # If the open tab IS the loaded manuscript, there's really only one
    # candidate — don't ask "which document?" and don't re-download it.
    tab_is_manuscript = (
        has_current_file
        and bool(_current_doc_file_id)
        and current_file["id"] == _current_doc_file_id
    )
    # Genuine multi-candidate ambiguity: a *different* file is open in the
    # tab AND a manuscript is loaded.
    multi_candidate = has_manuscript and has_current_file and not tab_is_manuscript
    named = _match_named_file(message)

    # --- Keep / remove loaded-document requests -------------------------
    # "scan and keep this file" / "also load the supplement" → persist the
    # file across turns (not just a one-shot scan). Checked before the plain
    # scan triggers so a keep request never degrades to a single-turn scan.
    wants_remove = (
        _has_any(msg_lower, _REMOVE_ALL_MARKERS)
        or (_has_any(msg_lower, _REMOVE_VERBS)
            and (named or _has_any(msg_lower, _REMOVE_DOC_REFS)))
    )
    # "keep the manuscript loaded" / "keep the supplement in context" — the
    # exact-phrase markers above won't match every natural phrasing, so also
    # accept "keep" co-occurring with "loaded" or "in context". The combo
    # stays false-positive-safe: "keep this section concise" has no
    # "loaded"/"in context", so it doesn't trigger.
    wants_keep = (
        _has_any(msg_lower, _KEEP_MARKERS)
        or ("keep" in msg_lower and ("loaded" in msg_lower or "in context" in msg_lower))
        or ("keep" in msg_lower and named is not None)  # "keep <filename>"
    )
    if wants_remove:
        if _has_any(msg_lower, _REMOVE_ALL_MARKERS):
            return "remove_all"
        if named:
            return "remove_named"
        if has_current_file and _has_any(msg_lower, _REMOVE_DOC_REFS):
            return "remove_current"
        # removal requested but no target resolved → fall through; the model
        # will ask which file to remove.
    if wants_keep:
        if named:
            return "keep_named"
        # "keep the manuscript" — recognise the main-text intent before
        # falling back to the open tab. Uses a NARROWER ref set than
        # _MANUSCRIPT_REFS (which also lists "the supplement") so "keep the
        # supplement" with the supplement tab open routes to keep_current,
        # not to a no-op keep_manuscript.
        if has_manuscript and _has_any(msg_lower, ("manuscript", "the paper", "main text", "main manuscript")):
            return "keep_manuscript"
        if has_current_file and not tab_is_manuscript:
            return "keep_current"
        # keep requested but no target resolved → fall through to ask/scan.

    # --- Explicit scan/read request -------------------------------------
    has_trigger = any(trigger in msg_lower for trigger in _FOCUS_TRIGGERS)
    if has_trigger:
        wants_current = _has_any(msg_lower, _CURRENT_FILE_REFS)
        wants_manuscript = _has_any(msg_lower, _MANUSCRIPT_REFS)
        # An explicitly-named file beats a generic "this"/"the document".
        if named:
            return "focus_named"
        if wants_current and has_current_file and not tab_is_manuscript:
            return "focus_current"
        if wants_manuscript and has_manuscript:
            return "deep_manuscript"
        # Explicit scan but no target resolved.
        if multi_candidate:
            return "ask"  # two plausible targets — let the user pick
        # "scan this file/document" with no recognized tab — don't silently
        # fall back to the manuscript if there are other project files to pick
        # from; ask which one the user means.
        if wants_current and not has_current_file and _has_other_project_files():
            return "ask"
        if has_manuscript:
            return "deep_manuscript"  # only candidate available
        if has_current_file:
            return "focus_current"
        return "targeted"  # nothing to scan

    # --- Implicit "needs more context" signal ---------------------------
    # A coverage/existence question ("does the document mention X", "is there
    # a figure about Y", "find all …") explicitly signals the user expects a
    # document-grounded answer. When the term isn't in any chunk, targeted
    # retrieval would come up empty — so we proactively OFFER to scan (once
    # per session) rather than answer from thin excerpts.
    #
    # Pure low-confidence (no keyword overlap) is NOT used as a trigger: it
    # can't tell "needs the full doc" apart from "isn't about the doc at all"
    # (e.g. "what should I write in the response?"), and using it would nag
    # the user with scan offers / "which document?" on every off-keyword
    # question.
    query_words = set(re.findall(r"\b[a-zA-Z]{3,}\b", msg_lower))
    substantive = len(query_words) >= 3
    coverage = _has_any(msg_lower, _COVERAGE_REFS)
    needs_more = substantive and coverage

    if needs_more:
        score = best_chunk_score(message, doc) if has_manuscript else 0.0
        if multi_candidate:
            return "ask"  # could be the tab or the manuscript — ask which
        if has_manuscript:
            # Single candidate. If the coverage term is found in a chunk,
            # targeted retrieval is enough — don't offer. Otherwise the
            # answer likely needs the full doc → offer to scan.
            if score >= 0.15:
                return "targeted"
            return "offer_scan"
        if has_current_file:
            return "focus_current"

    return "targeted"


def _resolve_doc_choice(
    message: str, current_file: dict | None
) -> tuple[str, Optional[str], Optional[str]]:
    """Resolve the user's reply to a "which document?" clarification.

    Returns (kind, file_id, file_name) where kind is one of:
    - "focus_current"   : the browser-tab file (file_id/name set)
    - "deep_manuscript" : the loaded manuscript (file_id/name None)
    - "focus_named"     : a project file named in the reply (file_id/name set)
    - "ask_name"        : user said "another" but didn't name it — re-ask
    - "unclear"         : couldn't parse — re-ask
    """
    msg_lower = message.lower()
    has_current_file = bool(current_file and current_file.get("id"))

    named = _match_named_file(message)
    if named:
        return "focus_named", named[0], named[1]

    if _has_any(msg_lower, _CURRENT_FILE_REFS) and has_current_file:
        return "focus_current", current_file["id"], current_file.get("name", "unknown")

    if _has_any(msg_lower, _MANUSCRIPT_REFS):
        return "deep_manuscript", None, None

    if _has_any(msg_lower, ("another", "other", "different", "else", "not that", "not this")):
        return "ask_name", None, None

    return "unclear", None, None


def _manuscript_text_if_target(file_id: Optional[str]) -> Optional[str]:
    """If ``file_id`` is the loaded manuscript, return its in-memory full text.

    Lets a focus/scan request for the manuscript reuse the already-parsed
    ``_current_doc`` instead of re-downloading it from Drive (slow, and the
    most likely file to time out). Returns None when it's a different file
    or no manuscript is loaded.
    """
    if (
        file_id
        and _current_doc is not None
        and _current_doc_file_id
        and file_id == _current_doc_file_id
    ):
        return _current_doc.full_text
    return None


def _manuscript_is_kept() -> bool:
    """True when the loaded manuscript has also been added to _loaded_docs.

    In that case its full text is already injected every turn via the
    ## Loaded Documents block, so the per-turn manuscript chunk retrieval
    (and a one-shot deep-manuscript injection) would be redundant — callers
    use this to skip them.
    """
    return bool(_current_doc_file_id) and any(
        d["file_id"] == _current_doc_file_id for d in _loaded_docs
    )


def _add_loaded_doc(file_id: str, name: str, text: str) -> bool:
    """Add a file's text to the persistent loaded-documents set.

    Dedupes by file_id (re-keeping an already-kept file is a no-op). Returns
    True if newly added, False if it was already present. The text is stored
    verbatim (capped at inject time) so a later Drive edit doesn't silently
    invalidate it — but also means the user should re-keep after editing a
    file if they want the fresh content.
    """
    global _loaded_docs, _keep_ack
    for d in _loaded_docs:
        if d["file_id"] == file_id:
            return False  # already kept
    _loaded_docs.append({"file_id": file_id, "name": name, "text": text})
    _keep_ack = {"kind": "kept", "name": name}  # confirm the keep in _build_user_message
    logger.info(
        "Loaded-docs: added '%s' (%d chars); %d document(s) now kept",
        name, len(text), len(_loaded_docs),
    )
    return True


def _remove_loaded_doc_by_id(file_id: str) -> bool:
    """Drop a file from the loaded-documents set by Drive id. Returns True if removed."""
    global _loaded_docs
    before = len(_loaded_docs)
    _loaded_docs = [d for d in _loaded_docs if d["file_id"] != file_id]
    removed = len(_loaded_docs) < before
    if removed:
        logger.info("Loaded-docs: removed file_id=%s; %d remaining", file_id, len(_loaded_docs))
    return removed


def _is_kept_doc(file_id: Optional[str]) -> bool:
    """True if a file is already in the persistent loaded-documents set."""
    return bool(file_id) and any(d["file_id"] == file_id for d in _loaded_docs)


def _has_other_project_files() -> bool:
    """True if the project file index has any file other than the loaded manuscript.

    Used to decide whether "scan this document" (with no recognized tab) should
    ask which file the user means rather than silently scanning the manuscript.
    """
    if not _project_file_index:
        return False
    for entries in _project_file_index.values():
        for e in entries:
            fid = e.get("id")
            if fid and fid != _current_doc_file_id:
                return True
    return False


def _record_last_turn_scan(
    focused_content: Optional[str],
    full_manuscript_content: Optional[str],
    focus_id: Optional[str],
) -> None:
    """Record the current turn's scan injection so the meter reflects it.

    One-shot scans (focus_current/focus_named) and deep-manuscript scans inject
    content for a single turn that the persistent-state totals
    (_loaded_docs, scraped, history) don't capture. Without this the
    context-usage bar wouldn't move when the user scans a file. A focused file
    that's already kept in _loaded_docs is excluded here so it isn't
    double-counted (it's already in loaded_tokens).
    """
    global _last_turn_scan_chars
    chars = len(full_manuscript_content or "")
    if focused_content:
        already_kept = bool(focus_id) and any(
            d["file_id"] == focus_id for d in _loaded_docs
        )
        if not already_kept:
            chars += len(focused_content)
    _last_turn_scan_chars = chars


def _record_last_request(system_prompt: str, user_message: str) -> None:
    """Record the actual token count of the request just built.

    The panel meter shows this: it's the real size of the prompt sent to the
    model (system_prompt + user_message), so everything that counts — history,
    retrieved chunks, kept docs, one-shot scans, injected instructions — is
    captured by construction, with no double-counting (the message builder
    dedupes kept-vs-scanned files). One-shot scans raise it for that request
    and it drops when the next request is smaller, matching Claude Code.
    """
    global _last_request_tokens, _last_request_system_tokens, _last_request_user_tokens
    _last_request_system_tokens = _estimate_tokens(system_prompt)
    _last_request_user_tokens = _estimate_tokens(user_message)
    _last_request_tokens = _last_request_system_tokens + _last_request_user_tokens
    logger.info(
        "Context meter: last request ~%d tokens (system=%d, user=%d)",
        _last_request_tokens, _last_request_system_tokens, _last_request_user_tokens,
    )


async def _prepare_scan_context(
    message: str, current_file: dict | None
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Decide what document content to inject for this turn.

    Returns ``(focused_file_content, focused_file_name, focus_id,
    full_manuscript_content, clarification_text)``. ``clarification_text`` is
    set when the LLM should ask the user something instead of answering
    (either "which document?" or "want me to scan?") — in that case retrieval
    is skipped.

    - If we offered a quick scan last turn (``_awaiting_scan_confirmation``),
      the yes/no reply is resolved here and remembered for the session.
    - If we asked "which document?" last turn (``_awaiting_doc_choice``),
      the reply is resolved via ``_resolve_doc_choice``.
    - Otherwise the intent is classified by ``_classify_scan_intent`` and the
      appropriate content is fetched (or a clarification is requested).
    """
    global _awaiting_doc_choice, _awaiting_scan_confirmation, _scan_preference
    global _loaded_docs, _keep_ack
    # Reset the per-request keep-acknowledgment flag: if a keep happens this
    # turn _add_loaded_doc (or a manuscript-target branch below) sets it, and
    # _build_user_message consumes it. Clearing here prevents a stale flag from
    # a prior failed request from leaking into a normal turn.
    _keep_ack = None

    focused_file_content: Optional[str] = None
    focused_file_name: Optional[str] = None
    focus_id: Optional[str] = None
    full_manuscript_content: Optional[str] = None
    clarification_text: Optional[str] = None

    # --- Resolving a pending "want me to scan?" offer -------------------
    if _awaiting_scan_confirmation:
        _awaiting_scan_confirmation = False
        if _is_affirmative(message):
            _scan_preference = "yes"
            if _current_doc is not None:
                full_manuscript_content = _current_doc.full_text
                logger.info(
                    "Scan offer accepted — injecting full manuscript (%d chars); "
                    "future implicit-need questions will auto-scan", len(full_manuscript_content))
            return (focused_file_content, focused_file_name, focus_id,
                    full_manuscript_content, clarification_text)
        if _is_negative(message):
            _scan_preference = "no"
            logger.info("Scan offer declined — using targeted chunks; "
                        "future implicit-need questions will use chunks")
            return (focused_file_content, focused_file_name, focus_id,
                    full_manuscript_content, clarification_text)
        # Neither yes nor no — treat as a fresh question (don't set a
        # preference) and fall through to normal classification.

    # --- Resolving a pending "which document?" question -----------------
    if _awaiting_doc_choice:
        kind, fid, fname = _resolve_doc_choice(message, current_file)
        if kind in ("focus_current", "focus_named") and fid:
            _awaiting_doc_choice = False
            # If they picked the manuscript, use the in-memory parse (capped
            # Full Manuscript path) instead of re-downloading from Drive.
            ms_text = _manuscript_text_if_target(fid)
            if ms_text is not None:
                full_manuscript_content = ms_text
            else:
                focused_file_name = fname
                focus_id = fid
                focused_file_content = await _download_and_parse_file(fid, fname)
            return (focused_file_content, focused_file_name, focus_id,
                    full_manuscript_content, clarification_text)
        if kind == "deep_manuscript" and _current_doc is not None:
            _awaiting_doc_choice = False
            full_manuscript_content = _current_doc.full_text
            return (focused_file_content, focused_file_name, focus_id,
                    full_manuscript_content, clarification_text)
        if kind == "ask_name":
            # User said "another" but didn't name it — keep awaiting, re-ask.
            clarification_text = _build_clarification_block(current_file)
            return (focused_file_content, focused_file_name, focus_id,
                    full_manuscript_content, clarification_text)
        # "unclear" — the user likely ignored the question and asked
        # something else. Stop awaiting and treat this as a fresh message so
        # we don't trap them in a "which document?" loop.
        _awaiting_doc_choice = False
        # fall through to normal classification below

    # --- Normal turn — classify intent ----------------------------------
    intent = _classify_scan_intent(message, current_file, _current_doc, _awaiting_doc_choice)

    # --- Keep / remove persistent loaded documents ----------------------
    # These mutate the _loaded_docs set; the actual text is injected every
    # turn by the ## Loaded Documents block in _build_user_message, so on a
    # successful keep we don't also set focused_file_content — that would
    # double-inject. Retrieval still runs (targeted) so the model has its
    # usual chunks alongside the newly-kept file.
    if intent == "keep_named":
        named = _match_named_file(message)
        if named:
            fid, fname = named
            if _manuscript_text_if_target(fid) is not None:
                # The named file IS the loaded manuscript — promote it from
                # chunked _current_doc to full-text kept (every turn). Reuse the
                # already-parsed text; no re-download. _add_loaded_doc dedupes by
                # file_id, so re-pressing scan is idempotent, and sets the
                # "kept" ack. Chunk retrieval is suppressed downstream when kept.
                ms_text = _manuscript_text_if_target(fid)
                _add_loaded_doc(_current_doc_file_id, _current_doc_file_name or fname, ms_text)
                logger.info("Loaded-docs: '%s' is the manuscript; kept in full text", fname)
            else:
                text = await _download_and_parse_file(fid, fname)
                if text:
                    _add_loaded_doc(fid, fname, text)
                else:
                    clarification_text = (
                        "## File Scan Failed\n"
                        f"The user asked to keep **{fname}** loaded, but the server "
                        f"couldn't download/parse it (the file is likely too large "
                        f"and the read timed out). Tell the user honestly and suggest "
                        f"they click **Load Project** again, then retry."
                    )
    elif intent == "keep_current":
        if current_file and current_file.get("id"):
            fid = current_file["id"]
            fname = current_file.get("name", "unknown")
            if _manuscript_text_if_target(fid) is not None:
                ms_text = _manuscript_text_if_target(fid)
                _add_loaded_doc(_current_doc_file_id, _current_doc_file_name or fname, ms_text)
                logger.info("Loaded-docs: current tab '%s' is the manuscript; kept in full text", fname)
            else:
                text = await _download_and_parse_file(fid, fname)
                if text:
                    _add_loaded_doc(fid, fname, text)
                else:
                    clarification_text = (
                        "## File Scan Failed\n"
                        f"The user asked to keep **{fname}** loaded, but the server "
                        f"couldn't download/parse it. Tell the user honestly and "
                        f"suggest they click **Load Project** again, then retry."
                    )
    elif intent == "keep_manuscript":
        # Promote the manuscript from chunked _current_doc to full-text kept.
        if _current_doc is not None and _current_doc_file_id:
            _add_loaded_doc(
                _current_doc_file_id,
                _current_doc_file_name or "the manuscript",
                _current_doc.full_text,
            )
            logger.info("Loaded-docs: manuscript kept in full text")
    elif intent == "remove_named":
        named = _match_named_file(message)
        if named:
            _remove_loaded_doc_by_id(named[0])
    elif intent == "remove_current":
        if current_file and current_file.get("id"):
            _remove_loaded_doc_by_id(current_file["id"])
    elif intent == "remove_all":
        n = len(_loaded_docs)
        _loaded_docs = []
        logger.info("Loaded-docs: cleared all (%d removed)", n)

    if intent == "focus_named":
        named = _match_named_file(message)
        if named:
            fid, fname = named
            ms_text = _manuscript_text_if_target(fid)
            if ms_text is not None:
                # Named file is the loaded manuscript — use in-memory parse.
                if _manuscript_is_kept():
                    # Already kept in full (## Loaded Documents every turn) —
                    # a one-shot inject would duplicate it. Skip.
                    logger.info("Focus file: '%s' is the manuscript and already kept; skipping one-shot inject", fname)
                else:
                    full_manuscript_content = ms_text
            elif _is_kept_doc(fid):
                # Already kept — injected every turn via the Loaded Documents
                # block. Don't re-inject as a one-shot focused file: it would
                # duplicate the content in the prompt (and double-count in the
                # meter). Dedup by document id, per the meter's contract.
                logger.info("Focus file: '%s' already kept; skipping one-shot inject", fname)
            else:
                focus_id, focused_file_name = fid, fname
                focused_file_content = await _download_and_parse_file(fid, fname)
                # A named-file scan that fails to download (the usual cause is
                # a large file timing out) must NOT silently degrade to 3-chunk
                # retrieval — that leaves the model claiming it "can't see" a
                # file the user explicitly named. Surface the failure honestly
                # so the model says so and suggests re-loading the project.
                if focused_file_content is None:
                    clarification_text = (
                        "## File Scan Failed\n"
                        f"The user asked to scan **{fname}**, but the server "
                        f"couldn't download/parse it (the file is likely too "
                        f"large and the read timed out). Do NOT pretend you "
                        f"can see it, and do NOT answer from the partial "
                        f"excerpts below. Tell the user honestly that the scan "
                        f"failed and suggest they click **Load Project** again "
                        f"(which parses the manuscript in-memory once, avoiding "
                        f"the per-scan re-download), then retry."
                    )
                    focus_id = None
                    focused_file_name = None
    elif intent == "focus_current":
        if current_file and current_file.get("id"):
            fid = current_file["id"]
            ms_text = _manuscript_text_if_target(fid)
            if ms_text is not None:
                # The tab file is the loaded manuscript — use in-memory parse.
                if _manuscript_is_kept():
                    logger.info("Focus file: current tab '%s' is the manuscript and already kept; skipping one-shot inject",
                                current_file.get("name", "unknown"))
                else:
                    full_manuscript_content = ms_text
            elif _is_kept_doc(fid):
                # Already kept — injected every turn via the Loaded Documents
                # block; skip the one-shot inject (dedup by document id).
                logger.info("Focus file: current tab '%s' already kept; skipping one-shot inject",
                            current_file.get("name", "unknown"))
            else:
                focus_id = fid
                focused_file_name = current_file.get("name", "unknown")
                focused_file_content = await _download_and_parse_file(fid, focused_file_name)
                # Download can time out on large files (the manuscript is the
                # usual culprit). Don't let that silently degrade a "scan this
                # document" request to 3-chunk excerpts — fall back to the
                # in-memory manuscript when one is loaded.
                if focused_file_content is None and _current_doc is not None:
                    logger.warning(
                        "Focus file: download of '%s' failed; falling back to "
                        "in-memory manuscript (%d chars)",
                        focused_file_name, len(_current_doc.full_text),
                    )
                    full_manuscript_content = _current_doc.full_text
                    focus_id = None
                    focused_file_name = None
    elif intent == "deep_manuscript":
        if _manuscript_is_kept():
            # Already kept in full (in ## Loaded Documents every turn) — a
            # one-shot full-manuscript inject would duplicate it. Skip and let
            # normal retrieval (comments + chat) run.
            logger.info("Full-manuscript scan requested but manuscript already kept; skipping one-shot inject")
        elif _current_doc is not None:
            full_manuscript_content = _current_doc.full_text
            logger.info(
                "Full-manuscript scan requested (%d chars) — bypassing chunk retrieval",
                len(full_manuscript_content),
            )
    elif intent == "ask":
        clarification_text = _build_clarification_block(current_file)
        _awaiting_doc_choice = True
    elif intent == "offer_scan":
        # Implicit need for the full doc, single manuscript. Honour the
        # remembered session preference if any; otherwise offer once.
        if _scan_preference == "yes" and _current_doc is not None:
            full_manuscript_content = _current_doc.full_text
            logger.info(
                "Auto-scan (preference=yes): injecting full manuscript (%d chars)",
                len(full_manuscript_content))
        elif _scan_preference == "no":
            # User previously declined — answer from targeted chunks.
            pass
        else:
            clarification_text = _build_scan_offer_block()
            _awaiting_scan_confirmation = True
    # "targeted" → nothing to inject; retrieve_context handles it

    logger.info(
        "Scan routing: intent=%s | focused_file=%s | full_manuscript=%s chars | "
        "clarification=%s | scan_pref=%s",
        intent,
        focused_file_name or "(none)",
        len(full_manuscript_content) if full_manuscript_content else 0,
        bool(clarification_text),
        _scan_preference or "(unset)",
    )

    return (focused_file_content, focused_file_name, focus_id,
            full_manuscript_content, clarification_text)


def _available_document_names(current_file: dict | None) -> list[str]:
    """Names of documents the user could choose to scan, for the clarification prompt."""
    names: list[str] = []
    seen: set[str] = set()
    if current_file and current_file.get("name"):
        names.append(current_file["name"])
        seen.add(current_file["name"].lower())
    if _current_doc is not None and _current_doc.title:
        t = _current_doc.title
        if t.lower() not in seen:
            names.append(t)
            seen.add(t.lower())
    # Project files (names only) — deduped, capped so the prompt stays small.
    for entries in _project_file_index.values():
        for e in entries:
            n = e.get("name", "")
            if n and n.lower() not in seen:
                seen.add(n.lower())
                names.append(n)
        if len(names) >= 30:
            break
    return names


def _build_clarification_block(current_file: dict | None) -> str:
    """The injected instruction that makes the LLM ask which document to scan."""
    lines = [
        "## Clarification Needed",
        "",
        "The user's question appears to need the full content of a specific document, "
        "but it isn't clear which one. Do NOT attempt to answer from any partial "
        "excerpts — ask the user which document to scan first, then wait for their reply.",
        "",
    ]
    names = _available_document_names(current_file)
    if names:
        lines.append("Documents available to scan:")
        for n in names:
            lines.append(f"- {n}")
        lines.append("")
    lines.append(
        "Ask something like: \"Do you want me to scan the document you're currently "
        "viewing, the loaded manuscript, or another project file? If another, tell me "
        "its name.\" Then wait — do not answer the original question yet."
    )
    return "\n".join(lines)


def _build_scan_offer_block() -> str:
    """The injected instruction that makes the LLM offer a quick deep scan."""
    return "\n".join([
        "## Proactive Scan Offer",
        "",
        "The user's question looks like it would benefit from the full document, but "
        "the targeted excerpts in context may not be enough to answer it well — and the "
        "user hasn't explicitly asked you to scan.",
        "",
        "Offer a quick scan and wait for a yes/no. Ask something like: \"Do you want me "
        "to scan this document really quickly so I can answer with the full context?\" "
        "Then STOP — do not attempt to answer the original question yet. If they say "
        "yes, the full document will be provided next turn; if no, answer from what you "
        "have.",
    ])


_YES_TOKENS = ("yes", "yeah", "yep", "sure", "please", "go ahead", "do it", "okay", "ok")
_NO_TOKENS = ("no", "nope", "nah", "skip", "later", "not now", "don't", "dont")


def _is_affirmative(message: str) -> bool:
    m = message.lower().strip()
    return any(t in m for t in _YES_TOKENS)


def _is_negative(message: str) -> bool:
    m = message.lower().strip()
    return any(t in m for t in _NO_TOKENS)


# ---------------------------------------------------------------------------
# Project goal onboarding helpers
# ---------------------------------------------------------------------------


def _is_change_goal(low_message: str) -> bool:
    return any(p in low_message for p in _CHANGE_GOAL_PHRASES)


def _fire_and_forget(coro) -> None:
    """Schedule a coroutine on the running loop without awaiting it.

    Best-effort side effects (Drive sync, journal lookup) after a goal change.
    If there is no running event loop (e.g. unit tests calling the sync helpers
    directly), the coroutine is closed cleanly instead of leaking a
    "coroutine was never awaited" warning.
    """
    try:
        asyncio.create_task(coro)
    except RuntimeError:
        coro.close()


def _has_goal() -> bool:
    memory = get_current_memory()
    return bool(memory and memory.goal and memory.goal.mode)


# Free-text question for each (mode, subtype) — the field the answer fills.
def _goal_first_question(mode: str, subtype: str = "") -> tuple[str, str]:
    """Return (question_text, field_to_await) for the start of onboarding.

    field_to_await is "" when no free-text question is needed (the mode
    finalizes immediately, e.g. brainstorming).
    """
    if mode in ("paper_revision", "paper_writing"):
        return (
            "Which journal are you targeting for this paper? "
            "(e.g. Nature, Cell, NEJM — or 'skip' if you haven't decided yet)",
            "journal",
        )
    if mode == "grant":
        gt = subtype or "this"
        return (
            f"Got it — a {gt} grant. Is there anything specific I should know "
            "(funding mechanism / program, specific aims, deadline)? Type a note, "
            "or 'skip'.",
            "grant_details",
        )
    if mode == "application":
        if subtype == "other_professional" or not subtype:
            return (
                "Tell me briefly about this application — what it's for and who "
                "it's for (e.g. residency, fellowship, scholarship).",
                "freeform",
            )
        labels = {
            "job": "company or role",
            "med_school": "medical school",
            "grad_school": "graduate program or school",
        }
        whom = labels.get(subtype, "target")
        return (f"Which {whom} are you targeting?", "target")
    # brainstorming / paper_discussion / other → no questions
    return "", ""


async def _start_goal_onboarding(mode: str, subtype: str = "") -> dict:
    """Begin onboarding for a mode (called by POST /chat/goal).

    Returns {"question", "field"} when a free-text answer is needed, or
    {"recap"} when the mode needs no questions and is finalized immediately.
    """
    global _goal_onboarding, _awaiting_goal_field, _pending_goal, _goal_discussing
    _pending_goal = GoalState(mode=mode)
    _goal_discussing = False
    if mode == "grant":
        _pending_goal.grant_type = subtype or "other"
    elif mode == "application":
        _pending_goal.application_type = subtype or "other_professional"

    question, field = _goal_first_question(mode, subtype)
    if not field:
        # No free-text step — finalize right away.
        recap = await _finalize_goal()
        return {"recap": recap}

    _goal_onboarding = True
    _awaiting_goal_field = field
    return {"question": question, "field": field}


async def _advance_goal(message: str) -> Optional[dict]:
    """Capture the user's reply into the awaited field (does NOT finalize).

    Returns {"captured": True} when a field was captured, or None if nothing
    was awaited. Finalization (persist + reference lookup + recap) is done by
    the caller via _finalize_goal / _goal_finalize_stream, so the lookup can
    stream an interim "Looking up…" message instead of blocking silently.
    """
    global _awaiting_goal_field, _goal_discussing
    field = _awaiting_goal_field
    if not field or _pending_goal is None:
        return None

    msg = (message or "").strip()
    skip = msg.lower() in ("skip", "none", "no", "n/a", "na", "nothing", "")

    if field == "journal":
        if not skip:
            _pending_goal.journal = msg[:200]
    elif field == "grant_details":
        if not skip:
            _pending_goal.grant_details = msg[:500]
    elif field == "target":
        if not skip:
            _pending_goal.target = msg[:200]
    elif field == "freeform":
        _pending_goal.freeform = msg[:500]

    _awaiting_goal_field = ""
    _goal_discussing = False  # a concrete answer arrived — discussion is over
    return {"captured": True}


async def _sync_goal_to_drive(memory) -> None:
    """Best-effort Drive sync of the current memory (used after goal changes)."""
    try:
        if not memory.project_folder_id:
            return
        from drive_sync import _save_memory_to_drive

        await _save_memory_to_drive(memory.project_folder_id, memory.model_dump())
    except Exception as exc:
        logger.warning("Goal Drive sync failed (non-fatal): %s", exc)


def _goal_lookup_kind_name(goal: GoalState) -> Optional[tuple[str, str]]:
    """Return (kind, name) to look up for this goal, or None if no lookup
    applies (brainstorming / paper_discussion / other, or a missing target)."""
    if goal.mode in ("paper_revision", "paper_writing") and goal.journal:
        return "journal", goal.journal
    if goal.mode == "application" and (goal.target or goal.freeform):
        return "program", (goal.target or goal.freeform)
    if goal.mode == "grant" and goal.grant_type:
        name = goal.grant_type
        if goal.grant_details:
            name = f"{goal.grant_type} ({goal.grant_details})"
        return "grant", name
    return None


async def _lookup_and_store_context(goal: GoalState) -> None:
    """Best-effort reference lookup for the goal's target, stored into the goal.

    Picks the lookup kind by mode (journal / program / grant) and stores notes
    + ok + source URL into the goal's fields. The caller (_finalize_goal)
    persists and syncs after. Never raises.
    """
    kn = _goal_lookup_kind_name(goal)
    if not kn:
        return
    kind, name = kn
    if goal.mode in ("paper_revision", "paper_writing"):
        set_fields = ("journal_formatting", "journal_lookup_ok", "journal_source_url")
    else:
        set_fields = ("target_info", "target_info_ok", "target_info_url")

    try:
        from memory_digest import fetch_context_info

        note, ok, url = await fetch_context_info(kind, name)
    except Exception as exc:
        logger.warning("Context lookup failed (non-fatal): %s", exc)
        return

    setattr(goal, set_fields[0], note)
    setattr(goal, set_fields[1], ok)
    setattr(goal, set_fields[2], url)
    logger.info(
        "Context lookup (%s) for '%s' (ok=%s, %d chars, url=%s)",
        kind, name, ok, len(note), url or "(none)",
    )


async def _finalize_goal() -> str:
    """Persist _pending_goal into memory.goal and return the user-facing recap.

    For modes with a target (journal / program / grant), the reference lookup
    is awaited (capped at ~60s — one LLM call, bounded by model latency)
    BEFORE building the recap, so the recap can cite the source URL. On
    timeout/failure the recap falls back to a "couldn't fetch" note and the
    model uses its own knowledge. Callers that stream (the /chat/send capture
    path) show an interim "Looking up…" message first via _goal_finalize_stream.
    """
    global _goal_onboarding, _awaiting_goal_field, _pending_goal, _goal_discussing
    memory = get_current_memory()
    if memory is None or _pending_goal is None:
        _goal_onboarding = False
        _awaiting_goal_field = ""
        _pending_goal = None
        _goal_discussing = False
        return "Goal saved."

    g = _pending_goal
    g.created = datetime.now(timezone.utc).isoformat()
    memory.goal = g

    # Awaited reference lookup so the recap can name the source URL. Best-effort
    # with a generous timeout (one LLM call; no network scraping).
    try:
        await asyncio.wait_for(_lookup_and_store_context(g), timeout=60)
    except asyncio.TimeoutError:
        logger.info("Context lookup timed out at finalize for '%s'", g.mode)
    except Exception as exc:
        logger.warning("Context lookup failed at finalize: %s", exc)

    _save_local(memory)
    recap = goal_recap_text(memory.goal)

    # Best-effort, non-blocking Drive sync.
    _fire_and_forget(_sync_goal_to_drive(memory))

    _goal_onboarding = False
    _awaiting_goal_field = ""
    _pending_goal = None
    _goal_discussing = False
    return recap


async def _goal_finalize_stream():
    """Stream the goal finalization as SSE so the user sees an interim
    "Looking up…" message while the (potentially slow) reference LLM lookup
    runs, then the final recap. Emits the recap with replace=true so it
    replaces (not appends to) the interim line in the assistant bubble.
    """
    g = _pending_goal
    kn = _goal_lookup_kind_name(g) if g else None
    if kn:
        kind, name = kn
        label = {"journal": "formatting guidelines", "program": "program info",
                 "grant": "grant info"}.get(kind, "info")
        yield f"data: {json.dumps({'type': 'text', 'content': f'Looking up {label} for {name}…'})}\n\n"
    recap = await _finalize_goal()
    # replace=true → the panel resets the bubble to just the recap (the interim
    # "Looking up…" line was transient).
    yield f"data: {json.dumps({'type': 'text', 'content': recap, 'replace': True})}\n\n"
    yield f"data: {json.dumps({'type': 'done'})}\n\n"


def _reset_goal_for_change() -> None:
    """Clear the saved goal and re-arm onboarding (for "change goal")."""
    global _goal_onboarding, _awaiting_goal_field, _pending_goal, _goal_discussing
    memory = get_current_memory()
    if memory is not None:
        memory.goal = None
        _save_local(memory)
        _fire_and_forget(_sync_goal_to_drive(memory))
    _goal_onboarding = True
    _awaiting_goal_field = ""
    _pending_goal = None
    _goal_discussing = False


async def _handle_goal_intercept(message: str) -> Optional[dict]:
    """If this message is a goal-onboarding turn, handle it and return a dict
    {"text": str, "show_buttons": bool} to emit as the response (bypassing the
    LLM). Returns None for normal messages.

    Requires an active memory (a loaded project); without one, goal commands
    are passed through to the LLM unchanged.
    """
    global _goal_discussing
    if get_current_memory() is None:
        return None

    low = (message or "").strip().lower()
    if _is_change_goal(low):
        _reset_goal_for_change()
        return {
            "text": (
                "Sure — let's set a new goal for this project. "
                "What would you like to work on?"
            ),
            "show_buttons": True,
        }

    if _goal_onboarding and _awaiting_goal_field:
        if _is_discussion_reply(message):
            # Not a concrete answer — the user wants to discuss before deciding.
            # Pass the message to the LLM for a guided chat (the system prompt
            # gets a "Goal Decision In Progress" hint). Do NOT call _advance_goal,
            # so the awaited field stays armed and the user's next real answer
            # (e.g. "UCLA") is captured + finalized normally.
            _goal_discussing = True
            return None
        result = await _advance_goal(message)
        if result and result.get("captured"):
            # Finalize via a streaming generator so the lookup can show an
            # interim "Looking up…" message (it may take 10-30s).
            return {"finalize_stream": True}
        return None

    return None


async def _single_message_stream(text: str, show_buttons: bool = False):
    """Emit one assistant message as an SSE stream (text + optional
    goal_buttons signal + done), so the onboarding intercept works with the
    existing streaming consumer in the side panel."""
    yield f"data: {json.dumps({'type': 'text', 'content': text})}\n\n"
    if show_buttons:
        yield f"data: {json.dumps({'type': 'goal_buttons'})}\n\n"
    yield f"data: {json.dumps({'type': 'done'})}\n\n"


def _build_user_message(
    message: str,
    include_paper: bool = True,
    include_comments: bool = True,
    focus_figure: Optional[str] = None,
    current_file: Optional[dict] = None,
    session_focus: Optional[str] = None,
    focused_file_content: Optional[str] = None,
    full_manuscript_content: Optional[str] = None,
    clarification_text: Optional[str] = None,
) -> str:
    """Build the enriched user message with retrieved context."""
    global _current_doc, _current_comments, _image_cache, _keep_ack

    parts = []

    # Session focus — the user's chosen area of work for this session.
    # Skipped when a persisted project goal is set (the goal's ## Project Goal
    # block in the system prompt already covers this, richer and durable).
    if session_focus and not _has_goal():
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

    # Clarification — either "which document?" (ambiguous target) or a
    # proactive "want me to scan?" offer (implicit need, single manuscript).
    # In both cases the LLM should ask and wait, NOT answer from partial
    # excerpts, so retrieval is skipped this turn.
    if clarification_text:
        parts.append(clarification_text)
        parts.append("---\n")

    # Full manuscript — injected when the user asks to scan/read the *loaded*
    # document (e.g. "scan the manuscript", "read the whole document") but
    # hasn't named a specific project file. Without this, a "scan" request
    # falls through to keyword retrieval, which only ever sends the top 3
    # matching chunks — so content the query keywords don't overlap (like a
    # newly added figure legend near the end) is never seen by the model.
    # Cap the body so a very large manuscript can't blow the context budget;
    # the truncation note tells the model (and user) the tail was dropped.
    # Skip entirely when the manuscript is already kept in full (## Loaded
    # Documents injects it every turn) — a one-shot Full Manuscript block
    # here would duplicate it.
    if full_manuscript_content and _current_doc is not None and not _manuscript_is_kept():
        cap = _doc_char_budget(0.5)  # scales with the model's context window
        body = full_manuscript_content
        truncation_note = ""
        if len(body) > cap:
            body = body[:cap]
            truncation_note = (
                f"\n\n[... TRUNCATED: this document is {len(full_manuscript_content):,} "
                f"characters but only the first {cap:,} (~{cap // 4:,} tokens) fit the "
                f"per-document budget for this model's context window. The remainder was "
                f"cut. Tell the user the file was only partially loaded, and that they "
                f"can ask about a specific section to retrieve content near the end.]"
            )
        parts.append(
            f"## Full Manuscript: {_current_doc.title}\n"
            f"The user asked to scan/read the FULL document. Below is the complete "
            f"text (with section headers). Treat this as the source of truth for "
            f"any question about the document's content — do not claim you cannot "
            f"see text that appears here.{truncation_note}\n\n"
        )
        parts.append(body)
        parts.append("\n---\n")

    # Focused file — full content injected when user asks to scan/read a file
    if focused_file_content and current_file and current_file.get("name"):
        fcap = _doc_char_budget(0.5)  # one-shot scan — same share as full-manuscript
        fbody = focused_file_content
        ftruncation = ""
        if len(fbody) > fcap:
            fbody = fbody[:fcap]
            ftruncation = (
                f"\n\n[... TRUNCATED: this file is {len(focused_file_content):,} characters "
                f"but only the first {fcap:,} (~{fcap // 4:,} tokens) fit the per-document "
                f"budget for this model's context window. The remainder was cut. Tell the "
                f"user the file was only partially loaded, and that they can ask about a "
                f"specific section to retrieve content near the end.]"
            )
        parts.append(
            f"## Focused File: {current_file['name']}\n"
            f"The user has asked to focus on this file. Below is the FULL content. "
            f"Read it carefully and be prepared to answer detailed questions about it.\n\n"
            f"Note: Figures, images, and embedded graphics may appear as garbled binary or "
            f"base64 text — you cannot parse those. Skip over them and focus on the "
            f"readable text content. If the user asks about a figure you cannot read, "
            f"tell them honestly and ask them to describe or paste the figure content.{ftruncation}\n"
        )
        parts.append(fbody)
        parts.append("---\n")
    elif current_file and current_file.get("name") and not full_manuscript_content:
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

    # Retrieved context — skipped when asking a clarification (we don't want
    # the model answering from partial chunks instead of asking).
    if not clarification_text and _current_doc and (include_paper or include_comments):
        memory = get_current_memory()
        chat_history = memory.chat_history if memory else []

        # When the full manuscript is already injected above (one-shot deep
        # scan) OR the manuscript is kept in full (in ## Loaded Documents),
        # skip the top-k paper-chunk retrieval (redundant) — still pull
        # reviewer comments and recent chat history.
        skip_paper_chunks = bool(full_manuscript_content) or _manuscript_is_kept()
        # Non-manuscript project docs are chunk-searched every turn with a
        # type-weighted budget (supplements/supporting). Exclude any file the
        # user already kept in _loaded_docs — its full text is injected
        # separately below, so chunk-retrieving it would be redundant.
        kept_ids = {d["file_id"] for d in _loaded_docs}
        extra_docs = [
            (d["type"], d["doc"])
            for d in _project_docs
            if d.get("file_id") not in kept_ids
        ]
        context = retrieve_context(
            query=message,
            doc=_current_doc,
            comments=_current_comments,
            chat_history=[t.model_dump() for t in chat_history],
            include_paper_chunks=not skip_paper_chunks,
            extra_docs=extra_docs,
            extra_budget={"supplement": 2, "supporting": 2, "miscellaneous": 1},
        )
        if context:
            parts.append(context)
            parts.append("---\n")

    # Loaded documents — project files the user asked to keep in context
    # across turns. Injected every turn (like web-scraped papers), capped
    # per-doc so a few large files don't blow the budget. Distinct from the
    # one-shot "Focused File" block above, which is a single-turn injection.
    if _loaded_docs:
        parts.append("## Loaded Documents\n")
        parts.append(
            "These project files are CURRENTLY kept loaded in context — this "
            "list is the source of truth for what is loaded right now. They "
            "stay available across turns until the user asks to remove them or "
            "the server restarts. Treat their text as source material alongside "
            "the manuscript. IMPORTANT: only quote/paraphrase text that is "
            "actually present below — never invent figure titles, legends, or "
            "captions. Long runs of blank lines are stripped embedded images, "
            "NOT missing text; scan the whole file before claiming any content "
            "is absent, and if you truly can't find it, say so honestly.\n"
        )
        cap = _doc_char_budget(0.25)  # kept docs are resent every turn — smaller share
        for d in _loaded_docs:
            body = d["text"]
            truncation_note = ""
            if len(body) > cap:
                body = body[:cap]
                truncation_note = (
                    f"\n\n[... TRUNCATED: this file is {len(d['text']):,} characters but "
                    f"only the first {cap:,} are kept in persistent context (per-document "
                    f"budget for this model). Tell the user this file was only partially "
                    f"loaded, and that they can ask about a specific section to see "
                    f"content near the end.]"
                )
            parts.append(f"### Kept File: {d['name']}\n{body}{truncation_note}\n")
            parts.append("---\n")
    else:
        # Explicit empty state so the model never infers "already loaded" from
        # resume memory when in fact nothing is kept (e.g. right after a restart).
        parts.append("## Loaded Documents\n")
        parts.append(
            "No project files are currently kept in context. Kept files live in "
            "server memory only and are cleared on restart — they are NOT "
            "restored from session memory. If the resume notes mention files kept "
            "in a prior session, those are no longer loaded. Do NOT tell the user "
            "a file is 'already loaded' or 'already kept'; if they ask to scan or "
            "keep a file, treat it as a fresh request.\n"
        )

    # Scan-and-keep confirmation: when a keep happened this turn, acknowledge
    # it and list EVERY file currently in context — the manuscript (if not
    # already kept) plus all kept docs — so the user gets a complete "here's
    # what's in context" signal and the model doesn't under-report the set.
    if _keep_ack:
        ack_name = _keep_ack["name"]
        # Full in-context listing. The manuscript is auto-loaded as _current_doc
        # (chunked) UNLESS it's been kept in full — in which case it already
        # appears in the kept list below, so don't list it twice.
        in_context: list[str] = []
        if _current_doc is not None and not _manuscript_is_kept():
            ms_label = _current_doc_file_name or "the main manuscript"
            in_context.append(f"{ms_label} (manuscript — auto-loaded, chunked)")
        in_context.extend(d["name"] for d in _loaded_docs)
        n = len(in_context)
        listing = "\n".join(
            f"{i + 1}. {label}" for i, label in enumerate(in_context)
        ) if in_context else "(none)"
        parts.append("## Scan Confirmation\n")
        parts.append(
            f"You just successfully scanned and kept **{ack_name}** in context. "
            f"Reply with a one-line confirmation naming this file, then list "
            f"EVERY file currently in context — there are {n} total and you must "
            f"name all {n}, omitting none:\n{listing}\n"
            f"Be brief by NOT dumping file contents; the file LIST must be "
            f"complete. Then wait for the user's next request.\n"
        )
        _keep_ack = None  # one-shot — only confirm on the keep turn

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
    "gemini":     "gemini-2.0-flash, gemini-2.5-flash, gemini-2.5-pro",
    "kimi":       "moonshot-v1-128k, kimi-k2-0905-preview, moonshot-v1-32k",
    # Local runtimes — model names depend on what the user has loaded.
    "local-ollama":   "llama3.1, qwen2.5, deepseek-r1 (whatever you `ollama pull`ed)",
    "local-lmstudio": "whatever model is loaded in the LM Studio GUI",
    "local-mlx":      "mlx-community/* model IDs (e.g. Llama-3.1-8B-Instruct-4bit)",
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

    # If it still looks like a raw JSON/dict or list repr, fall back to a generic message
    if cleaned.startswith("{") or cleaned.startswith("["):
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
            "gemini": "GEMINI_API_KEY",
            "kimi": "MOONSHOT_API_KEY",
        }
        key_name = provider_key_names.get(provider, "LLM_API_KEY")
        parts.append(
            f"\n\n🔐 Authentication failed for **{provider}**. "
            f"Make sure you're using your **{provider}** API key "
            f"({key_name}) — not a key from another provider. "
            f"Check the ⚙ Settings panel and re-enter the correct key."
        )

    # Local runtimes — the most common failure is the server not running /
    # wrong port, not an auth or model-name issue.
    if _is_local_provider(provider):
        is_conn = any(kw in msg_lower for kw in (
            "connection", "refused", "timeout", "timed out",
            "unreachable", "winerror 10061", "errno 111", "errno 61",
        ))
        if is_conn:
            parts.append(
                f"\n\n💡 Couldn't reach your local LLM runtime at the configured "
                f"Base URL. Is **{provider}** running? Start it (e.g. `ollama serve`), "
                f"check the port in the ⚙ Settings panel, and confirm a model is loaded."
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
        # 400, not 500: a missing API key / unconfigured provider is a client
        # setup issue, not a server crash. The detail from get_llm_config is
        # already actionable (points to ⚙ Settings / local LLM).
        raise HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

async def _stream_with_memory_save(
    stream: AsyncGenerator[str, None], user_message: str
) -> AsyncGenerator[str, None]:
    """Wrap a provider stream so the completed exchange is buffered to memory.

    Makes the streaming `/send` path self-contained: memory is persisted
    server-side on successful completion, with no dependence on a follow-up
    /memory/update request from the client. Forwards every SSE event
    unchanged; only acts on stream completion (and only if no error event
    was emitted).
    """
    accumulated: list[str] = []
    had_error = False
    async for payload in stream:
        yield payload
        if isinstance(payload, str) and payload.startswith("data: "):
            try:
                evt = json.loads(payload[len("data: "):].strip())
            except (json.JSONDecodeError, ValueError):
                continue
            etype = evt.get("type")
            if etype == "text":
                accumulated.append(evt.get("content", ""))
            elif etype == "error":
                had_error = True

    if had_error or not accumulated:
        return

    try:
        await update_memory_after_chat(user_message, "".join(accumulated))
    except Exception as exc:
        logger.warning("Streaming memory save failed (non-fatal): %s", exc)


@router.post("/send")
async def send_message(req: ChatRequest):
    """Send a message to the revision assistant (streaming SSE)."""
    provider = _get_provider()

    # Goal onboarding intercept — a "change goal" command, a pending
    # onboarding answer, or a just-captured answer that needs a streamed
    # finalization (interim "Looking up…" + recap). Handled without the LLM.
    goal_evt = await _handle_goal_intercept(req.message)
    if goal_evt is not None:
        if goal_evt.get("finalize_stream"):
            stream = _goal_finalize_stream()
        else:
            stream = _single_message_stream(
                goal_evt["text"], goal_evt.get("show_buttons", False)
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

    # Decide what document content to inject: resolve a pending "which
    # document?" reply, or classify this message's scan intent.
    (
        focused_file_content,
        focused_file_name,
        focus_id,
        full_manuscript_content,
        clarification_text,
    ) = await _prepare_scan_context(req.message, req.current_file)

    # Record this turn's scan injection so the context-usage meter reflects
    # one-shot/deep scans (done before building the system prompt so the
    # prompt's context-status guidance is current for this turn too).
    _record_last_turn_scan(focused_file_content, full_manuscript_content, focus_id)

    system_prompt = _build_system_prompt()
    user_message = _build_user_message(
        message=req.message,
        include_paper=req.include_paper_context,
        include_comments=req.include_reviewer_comments,
        focus_figure=req.focus_figure,
        current_file={"name": focused_file_name, "id": focus_id} if focused_file_name else req.current_file,
        session_focus=req.session_focus,
        focused_file_content=focused_file_content,
        full_manuscript_content=full_manuscript_content,
        clarification_text=clarification_text,
    )

    # Record the actual prompt size so the panel context meter reflects the
    # real next/last request (system + user), per the meter's contract.
    _record_last_request(system_prompt, user_message)

    logger.info(
        "Chat request [%s/%s]: '%s...' (system=%d, user=%d chars)",
        provider["provider"],
        provider["model"],
        req.message[:80],
        len(system_prompt),
        len(user_message),
    )

    # Summarize the full-text documents injected into this request so a
    # downstream API rejection (e.g. Gemini 400 INVALID_ARGUMENT) can be
    # traced to a specific document by name/size without re-deriving state.
    _ctx_docs = []
    if focused_file_content:
        _ctx_docs.append(f"focused={len(focused_file_content)}c")
    if full_manuscript_content:
        _ctx_docs.append(f"manuscript_scan={len(full_manuscript_content)}c")
    elif _current_doc is not None:
        _ctx_docs.append(
            f"manuscript='{_current_doc_file_name}'={len(_current_doc.full_text)}c"
        )
    for _d in _loaded_docs:
        _ctx_docs.append(f"kept='{_d.get('name', '?')}'={len(_d.get('text') or '')}c")
    for _sp in _scraped_docs:
        _ctx_docs.append(f"scraped='{_sp.title or '?'}'={len(_sp.full_text or '')}c")
    if _current_comments:
        _ctx_docs.append(
            f"comments={len(_current_comments)}"
            f"({sum(len(c.text) for c in _current_comments)}c)"
        )
    logger.info("Chat request context docs: %s", " | ".join(_ctx_docs) or "(none)")

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

    # Wrap so the exchange is buffered to memory on successful completion
    # (self-contained — no client follow-up needed).
    stream = _stream_with_memory_save(stream, req.message)

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

    # Goal onboarding intercept (see send_message). The streaming /send path
    # is the primary entry; /send-sync just awaits the full finalization.
    goal_evt = await _handle_goal_intercept(req.message)
    if goal_evt is not None:
        if goal_evt.get("finalize_stream"):
            recap = await _finalize_goal()
            return {
                "response": recap,
                "context_used": {
                    "provider": provider["provider"],
                    "model": provider["model"],
                    "goal_intercept": True,
                },
            }
        return {
            "response": goal_evt["text"],
            "goal_buttons": goal_evt.get("show_buttons", False),
            "context_used": {
                "provider": provider["provider"],
                "model": provider["model"],
                "goal_intercept": True,
            },
        }

    # Decide what document content to inject: resolve a pending "which
    # document?" reply, or classify this message's scan intent.
    (
        focused_file_content,
        focused_file_name,
        focus_id,
        full_manuscript_content,
        clarification_text,
    ) = await _prepare_scan_context(req.message, req.current_file)

    # Record this turn's scan injection so the context-usage meter reflects
    # one-shot/deep scans (done before building the system prompt so the
    # prompt's context-status guidance is current for this turn too).
    _record_last_turn_scan(focused_file_content, full_manuscript_content, focus_id)

    system_prompt = _build_system_prompt()
    user_message = _build_user_message(
        message=req.message,
        include_paper=req.include_paper_context,
        include_comments=req.include_reviewer_comments,
        focus_figure=req.focus_figure,
        current_file={"name": focused_file_name, "id": focus_id} if focused_file_name else req.current_file,
        session_focus=req.session_focus,
        focused_file_content=focused_file_content,
        full_manuscript_content=full_manuscript_content,
        clarification_text=clarification_text,
    )

    # Record the actual prompt size so the panel context meter reflects the
    # real next/last request (system + user), per the meter's contract.
    _record_last_request(system_prompt, user_message)

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

    # Update memory (await — Drive sync runs in thread pool)
    await update_memory_after_chat(
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
    provider: str = ""      # "anthropic" | "deepseek" | "glm" | "openai" | "gemini" | "kimi" | "custom"
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


class GoalRequest(BaseModel):
    mode: str  # paper_revision | paper_writing | application | grant | brainstorming | paper_discussion | other
    subtype: str = ""  # grant_type (NIH/NSF/...) or application_type (job/med_school/grad_school/other_professional)


@router.post("/goal")
async def set_goal(req: GoalRequest):
    """Start (or restart) the project-goal onboarding for the loaded project.

    The mode (+ optional subtype, button-chosen) is stored into the pending
    goal; the first free-text question is returned for the panel to show as an
    assistant message. Modes with no free-text step finalize immediately and
    return the recap. Requires an active memory (a loaded project).
    """
    if get_current_memory() is None:
        raise HTTPException(
            status_code=400,
            detail="Load a project first — the goal is saved per project.",
        )

    mode = (req.mode or "").strip()
    if mode not in (
        "paper_revision", "paper_writing", "application", "grant",
        "brainstorming", "paper_discussion", "other",
    ):
        raise HTTPException(status_code=400, detail=f"Unknown goal mode: {mode!r}")

    result = await _start_goal_onboarding(mode, (req.subtype or "").strip())
    if result.get("recap") is not None:
        return {"done": True, "recap": result["recap"], "goal": _goal_payload()}
    return {"done": False, "question": result["question"], "goal": _goal_payload()}


def _goal_payload() -> Optional[dict]:
    """Return the current saved goal as a dict (or None) for API responses."""
    from memory_manager import goal_payload

    return goal_payload()


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
                "id": "openai",
                "name": "OpenAI (GPT)",
                "sdk": "OpenAI SDK",
                "models": "gpt-4o, gpt-4-turbo, gpt-3.5-turbo, etc.",
                "env_vars": "LLM_API_KEY or OPENAI_API_KEY",
            },
            {
                "id": "anthropic",
                "name": "Anthropic (Claude)",
                "sdk": "Anthropic SDK",
                "models": "claude-sonnet-4-6, claude-opus-4-8, claude-haiku-4-5, etc.",
                "env_vars": "LLM_API_KEY or ANTHROPIC_API_KEY",
            },
            {
                "id": "gemini",
                "name": "Google (Gemini)",
                "sdk": "OpenAI-compatible",
                "models": "gemini-2.0-flash, gemini-2.5-flash, gemini-2.5-pro",
                "env_vars": "LLM_API_KEY or GEMINI_API_KEY",
            },
            {
                "id": "deepseek",
                "name": "DeepSeek",
                "sdk": "OpenAI-compatible",
                "models": "deepseek-v4-pro, deepseek-v4-flash",
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
                "id": "kimi",
                "name": "Moonshot AI (Kimi)",
                "sdk": "OpenAI-compatible",
                "models": "moonshot-v1-128k, kimi-k2-0905-preview, moonshot-v1-32k",
                "env_vars": "LLM_API_KEY or MOONSHOT_API_KEY",
            },
            {
                "id": "local-ollama",
                "name": "Ollama",
                "sdk": "OpenAI-compatible",
                "models": "llama3.1, qwen2.5, deepseek-r1, etc. (whatever you `ollama pull`ed)",
                "env_vars": "(none — local runtime, no key needed)",
            },
            {
                "id": "local-lmstudio",
                "name": "LM Studio",
                "sdk": "OpenAI-compatible",
                "models": "whatever model is loaded in the LM Studio GUI",
                "env_vars": "(none — local runtime, no key needed)",
            },
            {
                "id": "local-mlx",
                "name": "MLX Server",
                "sdk": "OpenAI-compatible",
                "models": "mlx-community/* model IDs (e.g. Llama-3.1-8B-Instruct-4bit)",
                "env_vars": "(none — local runtime, no key needed)",
            },
            {
                "id": "custom",
                "name": "Others (OpenAI-compatible)",
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
    "gemini-2.0-flash": 1048576,
    "moonshot-v1-128k": 131072,
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


def _doc_char_budget(tokens_fraction: float) -> int:
    """Per-document character budget scaled to the current model's context window.

    ``tokens_fraction`` of the window (in tokens) converted to chars at ~4
    chars/token. A one-shot scan can spend a larger share (one request) than a
    kept document (resent every turn). Scales with the model: on a 1M-token
    model a one-shot scan gets ~2M chars (so a typical supplement loads in
    full), on a 128k-token model ~256k. Floor of 20k chars so tiny windows
    don't regress to uselessness.
    """
    window_tokens, _ = _get_context_window_size()
    char_budget = int(window_tokens * tokens_fraction) * 4
    return max(20000, char_budget)


@router.get("/context-usage")
async def context_usage():
    """Estimate the context window usage of the next LLM request.

    Mirrors Claude Code's meter: if tokens are sent to the model, they count.
    Once at least one request has been built, the meter reflects the ACTUAL
    size of that request (system_prompt + user_message) — which includes
    history, retrieved chunks, kept docs, one-shot scans, and injected
    instructions, deduplicated at the source. A one-shot scan raises it for
    that request and it drops again when the next request is smaller.

    Before the first request (no measurement yet), fall back to a standing
    projection so the bar isn't empty on Load Project.
    """
    window_size, model = _get_context_window_size()

    if _last_request_tokens > 0:
        # Real measurement of the most recent request — the best estimate of
        # the next one's size. Breakdown sums to total_used by construction.
        total_used = _last_request_tokens
        breakdown = {
            "system_prompt": _last_request_system_tokens,
            "user_message": _last_request_user_tokens,
        }
        source = "last_request"
    else:
        # No request sent yet — project the standing baseline.
        ctx = _estimate_context_usage()
        total_used = ctx["total_used"]
        system_tokens = _estimate_tokens(SYSTEM_PROMPT)
        resume_tokens = _estimate_tokens(RESUME_PROMPT_EXTENSION)
        history_tokens = 0
        memory = get_current_memory()
        if memory and memory.chat_history:
            history_tokens = sum(_estimate_tokens(t.content) for t in memory.chat_history)
        scraped_tokens = sum(
            _estimate_tokens(doc.full_text[:6000]) + 200 for doc in _scraped_docs
        )
        loaded_tokens = sum(
            _estimate_tokens(d["text"][:_doc_char_budget(0.25)]) + 100 for d in _loaded_docs
        )
        breakdown = {
            "system_prompt": system_tokens,
            "resume_prompt": resume_tokens,
            "retrieval_chunks": 3 * 4000 // 4,
            "retrieval_comments": 5 * 500 // 4,
            "chat_history": history_tokens,
            "scraped_papers": scraped_tokens,
            "loaded_docs": loaded_tokens,
            "message_reserve": 8000,
        }
        source = "projected"

    pct_used = round(min((total_used / window_size) * 100, 100), 1)
    remaining = max(window_size - total_used, 0)

    return {
        "model": model,
        "window_size": window_size,
        "breakdown": breakdown,
        "source": source,
        "total_used": total_used,
        "remaining": remaining,
        "pct_used": pct_used,
        "pct_free": round(100 - pct_used, 1),
        "manuscript_available": _current_doc is not None,
        "manuscript_total_chars": len(_current_doc.full_text) if _current_doc else 0,
        "scraped_papers_count": len(_scraped_docs),
        "scraped_total_chars": sum(len(doc.full_text) for doc in _scraped_docs),
        "loaded_docs_count": len(_loaded_docs),
        "loaded_docs": [{"name": d["name"], "chars": len(d["text"])} for d in _loaded_docs],
        "project_docs_count": len(_project_docs),
        "project_docs": [
            {"name": d["name"], "type": d["type"], "chars": len(d["doc"].full_text)}
            for d in _project_docs
        ],
    }


@router.post("/refresh-context")
async def refresh_context():
    """Condense loaded context to memory, then drop it to free the window.

    The button next to the context bar. The big per-turn context consumers
    are the kept docs (_loaded_docs, injected in full every turn) and scraped
    papers (_scraped_docs). This endpoint:

    1. Condenses — runs the LLM digest (flush_memory_if_dirty) so the
       conversation about those docs becomes structured memory (decisions,
       summary, active_context), and records the reviewed file names in
       active_context so the model remembers what was looked at after the
       docs are gone.
    2. Drops — clears _loaded_docs, _scraped_docs, _scraped_sources, and the
       one-shot _focused_file_cache, plus stale scan-flow flags. The Loaded
       Documents and Scraped Articles panels empty and the window frees up.

    The displayed chat and the project baseline (manuscript _current_doc,
    project docs, comments) are left intact — use Clear Chat / Unload Project
    for those.
    """
    global _loaded_docs, _scraped_docs, _scraped_sources, _focused_file_cache
    global _keep_ack, _awaiting_doc_choice, _awaiting_scan_confirmation, _scan_preference
    global _last_request_tokens, _last_request_system_tokens, _last_request_user_tokens

    memory = get_current_memory()
    if memory is None:
        return {"status": "no_memory", "message": "No active session to refresh."}

    # Snapshot what's about to be dropped (read under no lock — atomic refs).
    dropped_doc_names = [d["name"] for d in _loaded_docs]
    dropped_scraped_titles = [doc.title or doc.__class__.__name__ for doc in _scraped_docs]
    focused_cleared = len(_focused_file_cache)

    # 1. Condense — digest any pending chat exchanges into structured memory.
    memory_flushed = False
    try:
        from memory_manager import flush_memory_if_dirty
        memory_flushed = await flush_memory_if_dirty()
    except Exception as exc:
        logger.warning("refresh-context: memory digest failed (non-fatal): %s", exc)

    # Record what was reviewed in active_context so it survives the drop.
    now = datetime.now(timezone.utc).isoformat()
    note_parts = [f"Context condensed {now[:10]} — dropped from active context:"]
    if dropped_doc_names:
        note_parts.append("  kept docs: " + ", ".join(dropped_doc_names))
    if dropped_scraped_titles:
        note_parts.append("  scraped articles: " + ", ".join(dropped_scraped_titles))
    if not dropped_doc_names and not dropped_scraped_titles:
        note_parts.append("  (no loaded docs or scraped articles were held)")
    note = "\n".join(note_parts)
    existing = (memory.active_context or "").strip()
    memory.active_context = (existing + "\n" + note) if existing else note
    memory.last_updated = now

    # Persist memory (local + Drive) BEFORE clearing the live context, so a
    # crash between the two can't lose the condensation.
    _save_local(memory)
    if memory.project_folder_id:
        try:
            from drive_sync import _save_memory_to_drive
            await _save_memory_to_drive(memory.project_folder_id, memory.model_dump())
        except Exception as exc:
            logger.warning("refresh-context: Drive sync failed (non-fatal): %s", exc)

    # 2. Drop — clear the heavy per-turn full-text injections + stale scan state.
    async with _state_lock:
        _loaded_docs = []
        _scraped_docs = []
        _scraped_sources = []
        _focused_file_cache = {}
        _keep_ack = None
        _awaiting_doc_choice = False
        _awaiting_scan_confirmation = False
        _scan_preference = ""
        _goal_onboarding = False
        _awaiting_goal_field = ""
        _pending_goal = None
        _goal_discussing = False
        # The last-request token measurement is now stale (the state it
        # measured is gone). Reset it so context_usage() falls back to the
        # standing projection — which recomputes from the cleared state and
        # shows the freed window immediately, instead of the old measurement
        # lingering until the next send.
        _last_request_tokens = 0
        _last_request_system_tokens = 0
        _last_request_user_tokens = 0

    logger.info(
        "refresh-context: dropped %d loaded doc(s), %d scraped article(s), %d focused cache entr(ies); memory_flushed=%s",
        len(dropped_doc_names), len(dropped_scraped_titles), focused_cleared, memory_flushed,
    )

    usage = await context_usage()

    return {
        "status": "refreshed",
        "memory_flushed": memory_flushed,
        "dropped_docs": dropped_doc_names,
        "dropped_docs_count": len(dropped_doc_names),
        "dropped_scraped": dropped_scraped_titles,
        "dropped_scraped_count": len(dropped_scraped_titles),
        "note": note,
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
        "loaded_docs": [{"name": d["name"], "chars": len(d["text"])} for d in _loaded_docs],
        "project_summary": _project_summary,
        "project_docs": [
            {"name": d["name"], "type": d["type"], "chars": len(d["doc"].full_text)}
            for d in _project_docs
        ],
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
    global _project_file_index, _project_structure
    global _project_docs, _project_summary
    global _awaiting_doc_choice
    global _awaiting_scan_confirmation, _scan_preference
    global _current_doc_file_id, _current_doc_file_name
    global _loaded_docs
    global _last_turn_scan_chars
    global _last_request_tokens, _last_request_system_tokens, _last_request_user_tokens
    global _goal_onboarding, _awaiting_goal_field, _pending_goal, _goal_discussing

    from memory_manager import (
        get_current_memory,
        set_current_memory,
        flush_memory_if_dirty,
        reset_pending,
    )

    memory_flushed = False
    async with _state_lock:
        # Track what we're clearing for the response
        existing_memory = get_current_memory()
        had_paper = _current_doc is not None
        had_comments = len(_current_comments)
        had_scraped = len(_scraped_docs)
        had_memory = existing_memory is not None
        had_loaded = len(_loaded_docs)

        # Flush pending chat exchanges to Drive before wiping — but only if
        # there's unsaved work, so an empty buffer restarts instantly with
        # no Drive round-trip.
        if existing_memory is not None:
            try:
                memory_flushed = await flush_memory_if_dirty()
            except Exception as exc:
                logger.warning("reset: memory flush failed (non-fatal): %s", exc)
            set_current_memory(None)
        reset_pending()

        _current_doc = None
        _current_comments = []
        _image_cache = {}
        _current_doc_source = ""
        _scraped_docs = []
        _scraped_sources = []
        _focused_file_cache = {}
        _project_file_index = {}
        _project_structure = []
        _project_docs = []
        _project_summary = ""
        _awaiting_doc_choice = False
        _awaiting_scan_confirmation = False
        _scan_preference = ""
        _goal_onboarding = False
        _awaiting_goal_field = ""
        _pending_goal = None
        _goal_discussing = False
        _current_doc_file_id = ""
        _current_doc_file_name = ""
        _loaded_docs = []
        _last_turn_scan_chars = 0
        _last_request_tokens = 0
        _last_request_system_tokens = 0
        _last_request_user_tokens = 0

    return {
        "status": "reset",
        "memory_flushed": memory_flushed,
        "cleared": {
            "manuscript": had_paper,
            "comments": had_comments,
            "scraped_papers": had_scraped,
            "memory": had_memory,
            "loaded_docs": had_loaded,
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
    global _project_file_index, _project_structure
    global _project_docs, _project_summary
    global _awaiting_doc_choice
    global _awaiting_scan_confirmation, _scan_preference
    global _current_doc_file_id, _current_doc_file_name
    global _loaded_docs
    global _last_turn_scan_chars
    global _last_request_tokens, _last_request_system_tokens, _last_request_user_tokens
    global _goal_onboarding, _awaiting_goal_field, _pending_goal, _goal_discussing

    from memory_manager import (
        get_current_memory,
        set_current_memory,
        flush_memory_if_dirty,
        reset_pending,
    )

    memory_flushed = False
    async with _state_lock:
        existing_memory = get_current_memory()
        had_paper = _current_doc is not None
        had_comments = len(_current_comments)
        had_memory = existing_memory is not None
        had_loaded = len(_loaded_docs)

        # Flush pending exchanges to Drive before dropping the project context
        # — only if there's unsaved work.
        if existing_memory is not None:
            try:
                memory_flushed = await flush_memory_if_dirty()
            except Exception as exc:
                logger.warning("unload-project: memory flush failed (non-fatal): %s", exc)
            set_current_memory(None)
        reset_pending()

        _current_doc = None
        _current_comments = []
        _image_cache = {}
        _focused_file_cache = {}
        _current_doc_source = ""
        _project_file_index = {}
        _project_structure = []
        _project_docs = []
        _project_summary = ""
        _awaiting_doc_choice = False
        _awaiting_scan_confirmation = False
        _scan_preference = ""
        _goal_onboarding = False
        _awaiting_goal_field = ""
        _pending_goal = None
        _goal_discussing = False
        _current_doc_file_id = ""
        _current_doc_file_name = ""
        _loaded_docs = []
        _last_turn_scan_chars = 0
        _last_request_tokens = 0
        _last_request_system_tokens = 0
        _last_request_user_tokens = 0

    return {
        "status": "unloaded",
        "memory_flushed": memory_flushed,
        "cleared": {
            "manuscript": had_paper,
            "comments": had_comments,
            "memory": had_memory,
            "loaded_docs": had_loaded,
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
        # The extension already fetched the HTML; a failure here is a parsing
        # error, not a fetch error. (A previous duplicate `except Exception`
        # here was unreachable and every failure returned a misleading 502
        # "Could not fetch the page".)
        logger.error("Scrape: failed to parse page %s: %s", req.url, exc)
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
