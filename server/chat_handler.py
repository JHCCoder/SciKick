"""Chat handler — multi-provider LLM integration with streaming, context injection.

Supported providers:
  - anthropic (Anthropic SDK)
  - deepseek, glm, openai, gemini, kimi, grok, minimax, qwen, custom (OpenAI-compatible SDK)
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

# Focused file cache — (file_id, figure_ocr) → parsed text content. Keyed by
# figure_ocr so a text-only focus result isn't served for an OCR scan-and-keep.
_focused_file_cache: dict[tuple[str, bool], str] = {}

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

# DeepSeek prefix-cache usage from the most recent request (0 until a provider
# response reports it). Exposed via /context-usage so the panel can show how
# much of the payload was served from the prefix cache at the cache-hit rate.
_last_cache_hit_tokens: int = 0
_last_cache_miss_tokens: int = 0

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
- **Never fabricate document content.** When you reference text from a loaded/kept document (the "## Loaded Documents" block), the manuscript, reviewer comments, or any scraped article, it must come from text actually present there — quote it or paraphrase it closely. Do NOT invent figure titles, figure legends, panel descriptions, section headings, captions, tables, or data values that you cannot locate in the provided text. Text *inside* a figure (axis labels, legend text, diagram labels, text in a screenshot) may have been recovered via OCR and appears as a "[Figure text (OCR): ...]" block (PDFs include a page number, e.g. "[Figure text (page N, OCR): ...]") — you CAN read and quote that. But a figure's purely visual content (exact data-point values, microscopy detail, color/shape relationships) is NOT available unless the user pastes the image into the chat — say so, rather than guessing what a figure depicts.
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

    # NOTE: the per-turn "Context Window Status" guidance deliberately lives in
    # the USER message tail (see _build_user_message), not here. The numbers
    # change every turn, and a changing token near the front of the request
    # would break DeepSeek's prefix cache for the entire stable payload below.
    # Keeping the system prompt byte-stable is what lets repeated paper/docs
    # context hit the cache at ~1/50th the input price.

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

    # NOTE: the "Session Resumed" digest block also moved to the user message
    # tail (_build_user_message) — it changes whenever the ~2-min digest runs,
    # and the same cache-prefix argument applies. The system prompt above is
    # now byte-stable across turns (modulo transient goal-onboarding states).

    return prompt


def _context_status_block() -> str:
    """The per-turn "Context Window Status" guidance for the model.

    Extracted so it can be appended at the very END of the user message rather
    than in the system prompt: the numbers change every turn, and any changing
    token before the stable document blocks would break the provider's prefix
    cache. ``include_transient=True`` so the model sees this turn's scan
    injection (the panel bar deliberately does NOT, to stay stable across
    turns).
    """
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
        return (
            f"\n\n## Context Window Status\n"
            f"Window: {ctx['window_size']:,} tokens | "
            f"In use: ~{ctx['total_used']:,} tokens ({ctx['pct_used']}%) | "
            f"Remaining: ~{ctx['remaining']:,} tokens.{guidance}"
        )
    except Exception:
        return ""


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


async def _download_and_parse_file(file_id: str, file_name: str, figure_ocr: bool = True, force: bool = False) -> str | None:
    """Download and parse a project file from Drive. Returns parsed text or None.

    ``figure_ocr`` (default True) is forwarded to parse_pdf / parse_docx —
    scan-and-keep wants the full per-image figure-OCR treatment; one-shot focus
    scans pass False to stay fast. The cache is keyed by (file_id, figure_ocr)
    so a text-only focus result is never served for an OCR keep (or vice versa).

    ``force`` (default False) bypasses the cache and re-downloads from Drive —
    used by the update-context endpoint so an "update" actually picks up Drive
    edits instead of returning the same cached bytes forever.
    """
    global _focused_file_cache
    cache_key = (file_id, figure_ocr)

    # Return cached content if available (unless the caller wants a fresh copy)
    if not force and cache_key in _focused_file_cache:
        logger.info("Focus file: using cached content for '%s' (%s, figure_ocr=%s)", file_name, file_id, figure_ocr)
        return _focused_file_cache[cache_key]

    try:
        from drive_sync import download_file

        logger.info("Focus file: downloading '%s' (%s)", file_name, file_id)
        downloaded = await download_file(file_id)
        mime = downloaded.get("mimeType", "")
        parsed_text = ""

        # Parsing (esp. per-image figure OCR) is CPU-heavy and synchronous —
        # running it on the event loop blocks /health and every other request
        # for the ~20-70s OCR takes, which the extension reads as a dead server
        # ("backend not found") and resets its tab state. Offload to a thread.
        if mime == "application/pdf" and "content_bytes" in downloaded:
            from file_processor import parse_pdf
            content_bytes = bytes.fromhex(downloaded["content_bytes"])
            doc = await asyncio.to_thread(parse_pdf, content_bytes, file_name, "auto", figure_ocr)
            parsed_text = doc.full_text
        elif mime == "application/vnd.google-apps.document" and "content_bytes" in downloaded:
            # Google Doc — now exported as PDF so embedded figures get OCR'd.
            from file_processor import parse_pdf
            content_bytes = bytes.fromhex(downloaded["content_bytes"])
            doc = await asyncio.to_thread(parse_pdf, content_bytes, file_name, "auto", figure_ocr)
            parsed_text = doc.full_text
        elif mime in ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",) and "content_bytes" in downloaded:
            from file_processor import parse_docx
            content_bytes = bytes.fromhex(downloaded["content_bytes"])
            doc = await asyncio.to_thread(parse_docx, content_bytes, file_name, figure_ocr)
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
            _focused_file_cache[cache_key] = parsed_text
            logger.info("Focus file: parsed and cached '%s' (%d chars, figure_ocr=%s)", file_name, len(parsed_text), figure_ocr)
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


# ---------------------------------------------------------------------------
# DeepSeek v4 thinking-mode heuristic
# ---------------------------------------------------------------------------

# Exact-match phrases that never need chain-of-thought.
_TRIVIAL_PHRASES = (
    "hi", "hello", "hey", "yo", "thanks", "thank you", "ok", "okay",
    "got it", "sounds good", "great", "perfect", "awesome", "cool",
    "nice", "fine", "done", "yes", "no",
)

# Greeting / chit-chat markers — skip thinking when combined with a short
# message (no substantive request attached).
_GREETING_MARKERS = (
    "hi", "hello", "hey", "yo", "good morning", "good afternoon",
    "good evening", "how are you", "thanks", "thank you",
)

# Analytical markers — always think, even in short messages. Checked before
# the short-factual skip so "why did the authors pick X?" keeps thinking.
_ANALYSIS_MARKERS = (
    "why", "explain", "analyze", "analyse", "compare", "contrast",
    "draft", "write", "revise", "edit", "rewrite", "respond to",
    "mechanism", "implications", "justify", "evaluate", "critique",
    "summarize", "summarise", "how does", "how do", "what causes",
    "what happens", "interpret", "synthesize", "synthesise", "argue",
    "hypothesis",
)

# Factual stems for short lookups ("what's the title?", "when published?").
_FACTUAL_STEMS = (
    "what is", "what are", "what's", "whats", "who", "when", "where",
    "how many", "how much", "is there", "are there", "does it", "which",
    "title of", "published", "what year", "what date",
)


def _wants_thinking(message: str, session_focus: Optional[str],
                    thinking_mode: str) -> bool:
    """Decide whether a request should run chain-of-thought (reasoning models).

    ``thinking_mode`` is the user's three-way toggle: "on" always thinks,
    "off" never does. "auto" (Balanced) skips thinking for trivial/short-
    factual requests and keeps it for anything analytical or substantive,
    mirroring the marker-tuple style of ``_classify_scan_intent``.
    """
    if thinking_mode == "on":
        return True
    if thinking_mode == "off":
        return False

    msg = message.strip()
    msg_lower = msg.lower()

    # Exact trivial acknowledgements — no thinking needed.
    if msg_lower in _TRIVIAL_PHRASES:
        return False

    # Analytical intent always thinks, even in short messages.
    if _has_any(msg_lower, _ANALYSIS_MARKERS):
        return True

    # Deep session focuses keep thinking on by default.
    if session_focus in ("brainstorming", "paper_discussion"):
        return True

    # Short greeting / chit-chat → skip thinking.
    if len(msg) < 40 and _has_any(msg_lower, _GREETING_MARKERS):
        return False

    # Short factual lookup ("what's the title?", "when was it published?")
    # → skip thinking.
    if len(msg) <= 60 and _has_any(msg_lower, _FACTUAL_STEMS):
        return False

    # Default: bias toward quality — think.
    return True


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
                focused_file_content = await _download_and_parse_file(fid, fname, figure_ocr=False)
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

    async def _keep_manuscript_ocr(label: str) -> None:
        """Scan-and-keep the manuscript with full figure OCR.

        The manuscript is parsed text-only at Load Project (figure OCR off for
        speed), so we can't just reuse ``_current_doc.full_text`` — that has no
        ``[Figure text (OCR): …]`` blocks. Re-download + re-parse with
        ``figure_ocr=True`` so the kept copy carries recovered figure text.
        Re-keep is a cheap no-op via ``_is_kept_doc`` (the OCR re-parse is the
        expensive part). On parse failure, surface the same File Scan Failed
        clarification the non-manuscript keeps use.
        """
        nonlocal clarification_text
        if not (_current_doc is not None and _current_doc_file_id):
            return
        if _is_kept_doc(_current_doc_file_id):
            logger.info("Loaded-docs: '%s' (manuscript) already kept; skipping OCR re-parse", label)
            return
        ms_text = await _download_and_parse_file(_current_doc_file_id, label, figure_ocr=True)
        if ms_text:
            _add_loaded_doc(_current_doc_file_id, _current_doc_file_name or label, ms_text)
            logger.info("Loaded-docs: '%s' is the manuscript; kept in full text with figure OCR", label)
        else:
            clarification_text = (
                "## File Scan Failed\n"
                f"The user asked to keep **{label}** loaded, but the server "
                f"couldn't download/parse it with OCR (the file may be too large "
                f"and the read timed out). Tell the user honestly and suggest they "
                f"click **Load Project** again, then retry."
            )

    if intent == "keep_named":
        named = _match_named_file(message)
        if named:
            fid, fname = named
            if _manuscript_text_if_target(fid) is not None:
                # The named file IS the loaded manuscript. It was parsed
                # text-only at load, so re-parse with figure OCR for the keep
                # (re-keep is a no-op). Chunk retrieval is suppressed downstream
                # when kept.
                await _keep_manuscript_ocr(fname)
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
                await _keep_manuscript_ocr(fname)
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
        # Promote the manuscript from chunked _current_doc to full-text kept,
        # re-parsed with figure OCR (load-time parse is text-only).
        await _keep_manuscript_ocr(_current_doc_file_name or "the manuscript")
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
                focused_file_content = await _download_and_parse_file(fid, fname, figure_ocr=False)
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
                focused_file_content = await _download_and_parse_file(fid, focused_file_name, figure_ocr=False)
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
    cache_split: bool = False,
) -> str | tuple[str, str, str]:
    """Build the enriched user message with retrieved context.

    With ``cache_split=True`` returns ``(full, stable, tail)`` instead of the
    plain string: ``stable`` is the byte-stable cross-turn prefix (kept docs +
    scraped pages) and ``tail`` is the per-turn remainder, with
    ``stable + tail == full`` byte-for-byte. Providers with explicit prompt
    caching (Anthropic) put a ``cache_control`` breakpoint on ``stable``.
    """
    global _current_doc, _current_comments, _image_cache, _keep_ack

    parts = []
    stable_end = 0  # split point after the stable Loaded-Docs / Scraped blocks

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
            f"Note: Text *inside* figures (axis labels, legend text, diagram labels, "
            f"text in screenshots) may have been recovered via OCR and appears as "
            f"\"[Figure text (OCR): ...]\" blocks — for PDFs these include a page "
            f"number, e.g. \"[Figure text (page N, OCR): ...]\". Either form is "
            f"figure text you can read and discuss. "
            f"Any remaining garbled binary or base64 text is raw image data you cannot "
            f"parse — skip over it. A figure's purely visual content (exact data-point "
            f"values, microscopy detail, color/shape relationships) is NOT available "
            f"unless the user pastes the image; if asked about that, say so honestly and "
            f"ask them to paste the figure.{ftruncation}\n"
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
            "captions. Text inside figures may have been OCR'd and appear as "
            "\"[Figure text (OCR): ...]\" blocks you can read (PDFs include a "
            "page number: \"[Figure text (page N, OCR): ...]\"). Long runs "
            "of blank lines are stripped embedded images, NOT missing text; scan "
            "the whole file before claiming any content is absent, and if you "
            "truly can't find it, say so honestly.\n"
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

    # Byte-stable prefix ends here: Loaded Documents + Web-Scraped Papers are
    # resent unchanged across turns, while everything from Retrieved Context
    # onward (retrieval, resume digest, the question, context status) changes
    # per turn. Providers with explicit prompt caching split at this boundary.
    stable_end = len(parts)

    # Retrieved context — skipped when asking a clarification (we don't want
    # the model answering from partial chunks instead of asking). Placed AFTER
    # the stable Loaded/Scraped blocks so the query-dependent retrieval text
    # sits in the changing tail of the request: the stable document prefix
    # above is what DeepSeek's cache serves at the cache-hit rate.
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
        # user already kept in _loaded_docs — its full text is already injected
        # above, so chunk-retrieving it would be redundant.
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

    # Resumed-session digest (conversation summary, decisions, comment status).
    # Changes only when the ~2-min digest runs, so it lives in the tail too —
    # a stable system prompt + stable document blocks are the cacheable prefix.
    memory = get_current_memory()
    if memory and memory.chat_history:
        parts.append(RESUME_PROMPT_EXTENSION)
        parts.append(build_resume_context())
        parts.append("---\n")

    # The user's actual message
    parts.append(f"## User Message\n{message}")

    # Context window awareness — model's own "how much room do I have" signal.
    # The numbers change every turn, so this must sit at the very tail: any
    # changing token before the stable blocks would break the cache prefix.
    ctx_status = _context_status_block()
    if ctx_status:
        parts.append(ctx_status)

    full = "\n".join(parts)
    if not cache_split:
        return full
    if stable_end == 0:
        return full, "", full
    # Trailing newline on the stable half keeps stable + tail == full exactly —
    # content blocks are rendered contiguously, so the model sees identical bytes.
    stable = "\n".join(parts[:stable_end]) + "\n"
    tail = "\n".join(parts[stable_end:])
    return full, stable, tail


# ---------------------------------------------------------------------------
# Provider-specific error enrichment
# ---------------------------------------------------------------------------

# Known valid models per provider — used to give helpful suggestions on 404 / invalid-model errors.
_PROVIDER_MODELS: dict[str, str] = {
    "anthropic":  "claude-opus-5, claude-sonnet-5, claude-haiku-4-5, claude-fable-5",
    "deepseek":   "deepseek-v4-pro, deepseek-v4-flash",
    "glm":        "glm-5.2, glm-5, glm-5.1, glm-4.7, glm-4.6, glm-4-long, glm-4.5-air",
    "openai":     "gpt-5, gpt-5.1, gpt-5.2, gpt-5.3, gpt-4.1, gpt-4o",
    "gemini":     "gemini-3.5-flash, gemini-3-pro, gemini-3.1-pro, gemini-2.5-flash, gemini-2.5-pro",
    "kimi":       "kimi-k3, kimi-k2.7-code, kimi-k2.6, kimi-k2.5",
    "grok":       "grok-4.5, grok-4.3, grok-4.20, grok-4.1-fast",
    "minimax":    "MiniMax-M3, MiniMax-M2.7, MiniMax-M2.5, MiniMax-M2.1",
    "qwen":       "qwen3-max, qwen3.5-plus, qwen3.5-flash, qwen-plus, qwen-flash",
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

# Providers that accept stream_options.include_usage and report cache token
# counts — others get no stream_options field at all (they may reject it).
_USAGE_PROVIDERS = {"deepseek", "openai", "gemini", "glm", "kimi", "grok", "minimax", "qwen"}

# Per-provider extra HTTP headers. Qwen/DashScope's session cache is opt-in
# (off by default) — this header enables it so repeated stable prefixes hit
# the cache. Empty for every other provider.
_PROVIDER_EXTRA_HEADERS: dict[str, dict[str, str]] = {
    "qwen": {"x-dashscope-session-cache": "enable"},
}

# Thinking-mode toggle: exact model ID -> param family. "on" = omit the param
# (all these families default to thinking on); the family entry in
# _THINKING_PARAMS stores the "off"/disable shape + a larger output budget
# where chain-of-thought needs room. Models not listed here can't toggle
# thinking (always-on / never-off) — the Auto/On/Off UI hides for them.
# Kept in sync by the update-models skill alongside _PROVIDER_MODELS.
_MODEL_THINKING_FAMILY: dict[str, str] = {
    # deepseek (v4 defaults to thinking on)
    "deepseek-v4-pro": "deepseek", "deepseek-v4-flash": "deepseek",
    # qwen — all 5 toggle via enable_thinking
    "qwen3-max": "qwen", "qwen3.5-plus": "qwen", "qwen3.5-flash": "qwen",
    "qwen-plus": "qwen", "qwen-flash": "qwen",
    # glm — glm-4-long is never-off, excluded
    "glm-5.2": "glm", "glm-5": "glm", "glm-5.1": "glm", "glm-4.7": "glm", "glm-4.6": "glm",
    "glm-4.5-air": "glm",
    # kimi — k3/k2.7-code can't toggle, excluded
    "kimi-k2.5": "kimi", "kimi-k2.6": "kimi",
    # openai — gpt-5 (minimal only) and gpt-4.x excluded; gpt-5.3 pending verify.
    # gpt-5.x omitting reasoning_effort defaults to NO reasoning (like qwen), so
    # the family has an explicit "on" shape (medium) — see _THINKING_PARAMS.
    "gpt-5.1": "openai", "gpt-5.2": "openai",
    # minimax — only M3; M2.x always think
    "MiniMax-M3": "minimax",
    # grok — only 4.3; 4.5 always-on, 4.20/4.1-fast are separate model IDs
    "grok-4.3": "grok",
    # NOTE: Gemini (2.5-flash etc.) is deliberately NOT here — its OpenAI-
    # compatible endpoint REJECTS the thinkingBudget field ("Unknown name
    # thinkingBudget"), so we can't toggle its thinking per-request. It keeps
    # its native always-on behavior; the Auto/On/Off buttons hide for it.
}

# Family -> how to toggle thinking. "off" is the disable shape; an optional
# "on" shape is sent when thinking is enabled for families that DON'T default
# to thinking on when the param is omitted (qwen: enable_thinking defaults to
# false). Families without "on" omit the param for on/auto-think (their
# natural default is on). A "max_tokens" gives CoT room when thinking.
_THINKING_PARAMS: dict[str, dict] = {
    "deepseek": {"off": {"thinking": {"type": "disabled"}}, "max_tokens": 32768},
    "qwen":     {"on": {"enable_thinking": True}, "off": {"enable_thinking": False}, "max_tokens": 32768},
    "glm":      {"off": {"thinking": {"type": "disabled"}}, "max_tokens": 32768},
    "kimi":     {"off": {"thinking": {"type": "disabled"}}, "max_tokens": 32768},
    "openai":   {"on": {"reasoning_effort": "medium"}, "off": {"reasoning_effort": "none"}},
    # MiniMax-M3's on-mode is "adaptive", NOT "enabled" (sending enabled 400s) —
    # omit-on sidesteps that entirely.
    "minimax":  {"off": {"thinking": {"type": "disabled"}}},
    "grok":     {"off": {"reasoning_effort": "none"}},
}

# Providers whose OpenAI-compatible API rejects the default 0.7 sampling
# temperature. A plain float applies to both thinking states; a
# {thinking: temp} dict lets a model require different temperatures per
# thinking mode — kimi-k2.x needs 1.0 while thinking, 0.6 with it disabled.
# Keyed by provider id; absent → 0.7. Kept in sync by update-models.
_TEMPERATURE_DEFAULTS: dict[str, float | dict[bool, float]] = {
    "kimi": {True: 1.0, False: 0.6},
    "openai": 1.0,  # gpt-5.x only accepts the default (1); gpt-4o/4.1 accept it too
}


def _sampling_temperature(provider: str, thinking: bool) -> float:
    """Return the sampling temperature for a provider + thinking state."""
    t = _TEMPERATURE_DEFAULTS.get(provider)
    if isinstance(t, dict):
        return t.get(thinking, 0.7)
    return t if t is not None else 0.7


def _max_tokens_kwarg(model: str) -> str:
    """OpenAI's gpt-5.x family rejects max_tokens — it requires max_completion_tokens."""
    return "max_completion_tokens" if model.startswith("gpt-5") else "max_tokens"


def _thinking_capable(model: str) -> bool:
    """Whether the given model supports the Auto/On/Off chain-of-thought toggle."""
    return model in _MODEL_THINKING_FAMILY


# Providers whose reasoning models emit chain-of-thought as literal
# <think>...</think> text in the content stream (MiniMax M3) instead of
# streaming it as reasoning_content. The block is stripped so the panel shows
# only the final answer. Extend as live smoke tests reveal more.
_THINK_TAG_PROVIDERS = {"minimax"}


class _ThinkStripper:
    """Drop <think>...</think> blocks from streamed text across chunk boundaries.

    MiniMax M3 wraps its chain-of-thought in literal <think> tags in
    delta.content (it has no reasoning_content), and a block can span many
    streamed chunks. We buffer while inside a block and only emit text outside
    it. An unclosed block (the model ran out mid-thought) is dropped at the end.
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"
    # Every proper prefix of _OPEN — if the buffer ends with one, the tag may
    # be split mid-word across chunks, so hold the tail back until we know.
    # (Literal string here: a class-body generator expression can't see the
    # class attribute `_OPEN`.)
    _PREFIXES = tuple("<think"[:i] for i in range(1, len("<think")))

    def __init__(self):
        self._buf = ""
        self._in_think = False

    def process(self, chunk: str) -> tuple[list[str], bool]:
        """Feed one content chunk; return (text to emit, newly_entered_think)."""
        self._buf += chunk
        out: list[str] = []
        entered = False
        while True:
            if self._in_think:
                end = self._buf.find(self._CLOSE)
                if end == -1:
                    break  # still inside the block — keep buffering
                self._buf = self._buf[end + len(self._CLOSE):]
                self._in_think = False
                continue
            start = self._buf.lower().find(self._OPEN)
            if start != -1:
                if start:
                    out.append(self._buf[:start])
                self._buf = self._buf[start + len(self._OPEN):]
                self._in_think = True
                entered = True
                continue
            # No complete open tag. If the buffer ends with a partial tag
            # prefix, hold the tail back; otherwise emit all of it.
            lower = self._buf.lower()
            held = 0
            for pref in self._PREFIXES:
                if lower.endswith(pref):
                    held = max(held, len(pref))
            if held:
                emit, self._buf = self._buf[:-held], self._buf[-held:]
            else:
                emit, self._buf = self._buf, ""
            if emit:
                out.append(emit)
            break
        return out, entered

    def flush(self) -> str:
        """Remaining buffer at stream end — an unclosed think block is dropped."""
        tail = self._buf
        self._buf = ""
        return "" if self._in_think else tail


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks from a complete (non-streamed) reply."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)


def _cache_counts_from_usage(usage, provider: str) -> tuple[int, int]:
    """Extract (cache_hit_tokens, cache_miss_tokens) from a provider usage obj.

    Field names differ per provider:
      - Anthropic:  cache_read_input_tokens (hits) vs input_tokens plus
        cache_creation_input_tokens (the paid cache write, uncached)
      - DeepSeek:   prompt_cache_hit_tokens / prompt_cache_miss_tokens
      - OpenAI/GLM/Gemini/Grok/MiniMax/Qwen (OpenAI-compat):
        prompt_tokens_details.cached_tokens (a subset of prompt_tokens);
        miss = prompt_tokens - hit
      - Kimi/Moonshot: bare usage.cached_tokens, else OpenAI-shaped details
    Returns (0, 0) when the provider doesn't report cache fields.
    """
    if usage is None:
        return 0, 0
    if provider == "anthropic":
        read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        created = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        uncached = int(getattr(usage, "input_tokens", 0) or 0)
        return read, created + uncached
    if provider in ("openai", "glm", "gemini", "kimi", "grok", "minimax", "qwen"):
        details = getattr(usage, "prompt_tokens_details", None)
        hit = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
        # Kimi/Moonshot also reports a bare usage.cached_tokens on the response.
        if not hit:
            hit = int(getattr(usage, "cached_tokens", 0) or 0)
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        return hit, max(prompt - hit, 0)
    # DeepSeek and anything unlisted: explicit hit/miss token fields.
    hit = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
    miss = int(getattr(usage, "prompt_cache_miss_tokens", 0) or 0)
    return hit, miss


def _record_cache_usage(model: str, usage, provider: str = "") -> None:
    """Capture prefix-cache hit/miss token counts and log them.

    The ratio tells us whether the cache-optimized prompt ordering is working —
    a high hit share means the stable prefix (system prompt + kept docs) is
    being served from cache at the cache-hit price. Providers without prefix
    caching simply lack the fields; nothing is recorded.
    """
    global _last_cache_hit_tokens, _last_cache_miss_tokens
    if usage is None:
        return
    hit, miss = _cache_counts_from_usage(usage, provider)
    _last_cache_hit_tokens = hit
    _last_cache_miss_tokens = miss
    if hit or miss:
        total = hit + miss
        logger.info(
            "Context cache [%s]: %d hit / %d miss tokens (%.1f%% cached)",
            model, hit, miss, (hit / total) * 100 if total else 0.0,
        )


def _anthropic_content_blocks(stable: str, tail: str) -> list[dict]:
    """Build Anthropic user content blocks with a cache breakpoint on the
    byte-stable prefix. ``stable + tail`` is the full user message; the stable
    block carries ``cache_control`` so repeated turns reuse it at the cache-hit
    price. Uses 1 of the 4 allowed breakpoints per request (the system block
    takes the other). An empty stable prefix degrades to a single block.
    """
    blocks: list[dict] = []
    if stable:
        blocks.append(
            {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}
        )
    if tail:
        blocks.append({"type": "text", "text": tail})
    return blocks or [{"type": "text", "text": ""}]


async def _stream_anthropic(
    stable: str, tail: str, system_prompt: str, model: str, api_key: str
) -> AsyncGenerator[str, None]:
    """Stream using the Anthropic SDK, with explicit prompt caching.

    ``cache_control`` breakpoints go on the system block and the byte-stable
    user-message prefix (kept docs + scraped pages) so repeated turns are
    served from Anthropic's prompt cache. Cache reads/writes are recorded so
    the context-usage meter shows the hit ratio.
    """
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=api_key)

    try:
        async with client.messages.stream(
            model=model,
            max_tokens=8192,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role": "user",
                "content": _anthropic_content_blocks(stable, tail),
            }],
        ) as stream:
            async for text in stream.text_stream:
                yield f"data: {json.dumps({'type': 'text', 'content': text})}\n\n"

        # The final message carries usage: cache_read_input_tokens (hits) and
        # cache_creation_input_tokens (the paid write on the first request).
        final = await stream.get_final_message()
        _record_cache_usage(model, getattr(final, "usage", None), "anthropic")

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    except Exception as exc:
        enriched = _enrich_error(str(exc), "anthropic", model)
        logger.error("Anthropic API error: %s", exc)
        yield f"data: {json.dumps({'type': 'error', 'content': enriched})}\n\n"


async def _stream_openai_compatible(
    message: str, system_prompt: str, model: str, api_key: str, base_url: str,
    provider: str = "",
    thinking: bool = True,
) -> AsyncGenerator[str, None]:
    """Stream using the OpenAI-compatible SDK (DeepSeek, OpenAI, Groq, etc.).

    ``thinking`` affects the models in ``_MODEL_THINKING_FAMILY`` (reasoning
    models that default to thinking mode ON): chain-of-thought streams in
    delta.reasoning_content while delta.content stays empty until the model
    finishes reasoning. Passing ``thinking=False`` disables it for fast
    answers to simple questions via the family's "off" param. Models not in
    the family map ignore ``thinking`` entirely.
    """
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    # The old 8192-token budget could be exhausted by thinking alone — the
    # stream then ended with ZERO content and the user saw a silent empty
    # reply. Give thinking + answer room, and stream a "thinking" marker so
    # the panel shows progress. With thinking disabled the plain 8192 budget
    # is plenty (no CoT consuming it).
    family = _MODEL_THINKING_FAMILY.get(model)
    params = _THINKING_PARAMS.get(family) if family else None
    max_tokens = params.get("max_tokens", 8192) if (params and thinking) else 8192
    # MiniMax-M3 streams its chain-of-thought as literal <think> tags in
    # content (no reasoning_content) — strip them so only the answer shows.
    stripper = _ThinkStripper() if provider in _THINK_TAG_PROVIDERS else None

    try:
        # Providers with prefix caching stream a final usage chunk (empty
        # choices) with cache hit/miss tokens when include_usage is set. Only
        # sent to providers known to accept it (DeepSeek/OpenAI/Gemini) —
        # others get no stream_options field at all.
        stream_kwargs = {}
        if provider in _USAGE_PROVIDERS:
            stream_kwargs["stream_options"] = {"include_usage": True}
        headers = _PROVIDER_EXTRA_HEADERS.get(provider)
        if headers:
            stream_kwargs["extra_headers"] = headers
        # "on"/auto-think = omit the param (these families default to thinking
        # on); "off" sends the family's disable shape. Models not in the family
        # map get no thinking param at all.
        if params:
            if not thinking:
                stream_kwargs["extra_body"] = params["off"]
            elif "on" in params:
                # Families whose default is thinking OFF (qwen: enable_thinking
                # omits to false) need the explicit on-shape; others omit.
                stream_kwargs["extra_body"] = params["on"]
        stream = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ],
            temperature=_sampling_temperature(provider, thinking),
            **{_max_tokens_kwarg(model): max_tokens},
            stream=True,
            **stream_kwargs,
        )

        usage = None
        yielded_text = False
        yielded_thinking = False
        async for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue
            if delta.content:
                if stripper is None:
                    yielded_text = True
                    yield f"data: {json.dumps({'type': 'text', 'content': delta.content})}\n\n"
                else:
                    pieces, entered = stripper.process(delta.content)
                    if entered and not yielded_thinking:
                        yielded_thinking = True
                        yield f"data: {json.dumps({'type': 'thinking', 'content': ''})}\n\n"
                    for piece in pieces:
                        if piece:
                            yielded_text = True
                            yield f"data: {json.dumps({'type': 'text', 'content': piece})}\n\n"
            elif not yielded_text and getattr(delta, "reasoning_content", None):
                # Reasoning model mid-thought — emit a one-shot progress marker
                # so the panel isn't dead air during a long chain-of-thought.
                if not yielded_thinking:
                    yielded_thinking = True
                    yield f"data: {json.dumps({'type': 'thinking', 'content': ''})}\n\n"

        if stripper is not None:
            leftover = stripper.flush()
            if leftover:
                yielded_text = True
                yield f"data: {json.dumps({'type': 'text', 'content': leftover})}\n\n"

        if not yielded_text:
            # Model consumed its output budget reasoning and returned no text —
            # surface it explicitly instead of a silent empty bubble.
            yield f"data: {json.dumps({'type': 'warning', 'content': 'The model returned an empty response — it spent its output budget reasoning and produced no text. Try rephrasing the question or asking about a narrower part of the document.'})}\n\n"

        # Capture how much of this request's input was served from the prefix
        # cache (reported in the final usage chunk by caching providers).
        _record_cache_usage(model, usage, provider)

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
    stable: str, tail: str, system_prompt: str, model: str, api_key: str
) -> str:
    """Non-streaming call via Anthropic SDK, with explicit prompt caching."""
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=api_key)
    response = await client.messages.create(
        model=model,
        max_tokens=8192,
        system=[{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{
            "role": "user",
            "content": _anthropic_content_blocks(stable, tail),
        }],
    )
    _record_cache_usage(model, getattr(response, "usage", None), "anthropic")
    return response.content[0].text


async def _sync_openai_compatible(
    message: str, system_prompt: str, model: str, api_key: str, base_url: str,
    provider: str = "",
    thinking: bool = True,
) -> str:
    """Non-streaming call via OpenAI-compatible SDK.

    ``thinking`` affects the models in ``_MODEL_THINKING_FAMILY`` (see
    ``_stream_openai_compatible`` for the reasoning-budget context).
    """
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    # Reasoning models default to thinking mode — reasoning consumes output
    # tokens before any visible content, so give them a much larger budget
    # than the generic 8192 (see _stream_openai_compatible for the full
    # context). With thinking disabled the plain 8192 budget is plenty (no CoT
    # consuming it).
    family = _MODEL_THINKING_FAMILY.get(model)
    params = _THINKING_PARAMS.get(family) if family else None
    max_tokens = params.get("max_tokens", 8192) if (params and thinking) else 8192
    headers = _PROVIDER_EXTRA_HEADERS.get(provider)
    create_kwargs = {"extra_headers": headers} if headers else {}
    # "on"/auto-think = omit the param (these families default to thinking on);
    # "off" sends the family's disable shape. Models not in the family map get
    # no thinking param at all.
    if params:
        if not thinking:
            create_kwargs["extra_body"] = params["off"]
        elif "on" in params:
            # Families whose default is thinking OFF (qwen: enable_thinking
            # omits to false) need the explicit on-shape; others omit.
            create_kwargs["extra_body"] = params["on"]
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ],
        temperature=_sampling_temperature(provider, thinking),
        **{_max_tokens_kwarg(model): max_tokens},
        **create_kwargs,
    )
    _record_cache_usage(model, getattr(response, "usage", None), provider)
    content = response.choices[0].message.content or ""
    # MiniMax-M3 wraps its chain-of-thought in literal <think> tags — strip
    # them so the non-streamed reply shows only the answer.
    if provider in _THINK_TAG_PROVIDERS:
        content = _strip_think_tags(content)
    return content


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

    # Anthropic places an explicit cache_control breakpoint on the byte-stable
    # prefix, so split it out there; the full string still feeds the meter and
    # logs, and the OpenAI-compatible path is unchanged.
    split_for_cache = _is_anthropic_provider(provider["provider"])
    built = _build_user_message(
        message=req.message,
        include_paper=req.include_paper_context,
        include_comments=req.include_reviewer_comments,
        focus_figure=req.focus_figure,
        current_file={"name": focused_file_name, "id": focus_id} if focused_file_name else req.current_file,
        session_focus=req.session_focus,
        focused_file_content=focused_file_content,
        full_manuscript_content=full_manuscript_content,
        clarification_text=clarification_text,
        cache_split=split_for_cache,
    )
    if split_for_cache:
        user_message, stable_prefix, volatile_tail = built
    else:
        user_message = built
        stable_prefix = volatile_tail = ""

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
            stable_prefix, volatile_tail, system_prompt,
            provider["model"], provider["api_key"],
        )
    else:
        thinking = _wants_thinking(
            req.message, req.session_focus,
            provider.get("thinking_mode", "auto"),
        )
        stream = _stream_openai_compatible(
            user_message,
            system_prompt,
            provider["model"],
            provider["api_key"],
            provider["base_url"],
            provider=provider["provider"],
            thinking=thinking,
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

    # Anthropic places an explicit cache_control breakpoint on the byte-stable
    # prefix, so split it out there; the full string still feeds the meter and
    # logs, and the OpenAI-compatible path is unchanged.
    split_for_cache = _is_anthropic_provider(provider["provider"])
    built = _build_user_message(
        message=req.message,
        include_paper=req.include_paper_context,
        include_comments=req.include_reviewer_comments,
        focus_figure=req.focus_figure,
        current_file={"name": focused_file_name, "id": focus_id} if focused_file_name else req.current_file,
        session_focus=req.session_focus,
        focused_file_content=focused_file_content,
        full_manuscript_content=full_manuscript_content,
        clarification_text=clarification_text,
        cache_split=split_for_cache,
    )
    if split_for_cache:
        user_message, stable_prefix, volatile_tail = built
    else:
        user_message = built
        stable_prefix = volatile_tail = ""

    # Record the actual prompt size so the panel context meter reflects the
    # real next/last request (system + user), per the meter's contract.
    _record_last_request(system_prompt, user_message)

    try:
        if _is_anthropic_provider(provider["provider"]):
            assistant_text = await _sync_anthropic(
                stable_prefix, volatile_tail, system_prompt,
                provider["model"], provider["api_key"],
            )
        else:
            thinking = _wants_thinking(
                req.message, req.session_focus,
                provider.get("thinking_mode", "auto"),
            )
            assistant_text = await _sync_openai_compatible(
                user_message,
                system_prompt,
                provider["model"],
                provider["api_key"],
                provider["base_url"],
                provider=provider["provider"],
                thinking=thinking,
            )
    except Exception as exc:
        enriched = _enrich_error(str(exc), provider["provider"], provider["model"])
        logger.error("LLM API error: %s", exc)
        raise HTTPException(status_code=502, detail=enriched)

    if not assistant_text:
        # Thinking-mode models can burn their whole output budget reasoning and
        # return zero content (see _stream_openai_compatible). Surface it rather
        # than silently recording an empty exchange.
        raise HTTPException(
            status_code=502,
            detail=(
                "The model returned an empty response — it spent its output budget "
                "reasoning and produced no text. Try rephrasing the question or "
                "asking about a narrower part of the document."
            ),
        )

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
    thinking_mode: str = ""  # "auto" | "on" | "off" (DeepSeek v4 chain-of-thought)
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
        thinking_mode=req.thinking_mode or None,
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
            "thinking_mode": current.get("thinking_mode", "auto"),
            "thinking_capable": _thinking_capable(current["model"]),
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


# Static display metadata for /chat/providers. `models` is NOT stored here —
# it's derived from _PROVIDER_MODELS (the single source of truth for model IDs),
# so a model refresh only edits that one table. `custom` has no _PROVIDER_MODELS
# entry and falls back to a free-form hint.
_PROVIDER_CATALOG: list[dict] = [
    {"id": "openai", "name": "OpenAI (GPT)", "sdk": "OpenAI SDK", "env_vars": "LLM_API_KEY or OPENAI_API_KEY"},
    {"id": "anthropic", "name": "Anthropic (Claude)", "sdk": "Anthropic SDK", "env_vars": "LLM_API_KEY or ANTHROPIC_API_KEY"},
    {"id": "gemini", "name": "Google (Gemini)", "sdk": "OpenAI-compatible", "env_vars": "LLM_API_KEY or GEMINI_API_KEY"},
    {"id": "deepseek", "name": "DeepSeek", "sdk": "OpenAI-compatible", "env_vars": "LLM_API_KEY or DEEPSEEK_API_KEY"},
    {"id": "glm", "name": "Zhipu AI (GLM)", "sdk": "OpenAI-compatible", "env_vars": "LLM_API_KEY or GLM_API_KEY"},
    {"id": "kimi", "name": "Moonshot AI (Kimi)", "sdk": "OpenAI-compatible", "env_vars": "LLM_API_KEY or MOONSHOT_API_KEY"},
    {"id": "grok", "name": "xAI (Grok)", "sdk": "OpenAI-compatible", "env_vars": "LLM_API_KEY or XAI_API_KEY"},
    {"id": "minimax", "name": "MiniMax", "sdk": "OpenAI-compatible", "env_vars": "LLM_API_KEY or MINIMAX_API_KEY"},
    {"id": "qwen", "name": "Alibaba (Qwen)", "sdk": "OpenAI-compatible", "env_vars": "LLM_API_KEY or DASHSCOPE_API_KEY"},
    {"id": "local-ollama", "name": "Ollama", "sdk": "OpenAI-compatible", "env_vars": "(none — local runtime, no key needed)"},
    {"id": "local-lmstudio", "name": "LM Studio", "sdk": "OpenAI-compatible", "env_vars": "(none — local runtime, no key needed)"},
    {"id": "local-mlx", "name": "MLX Server", "sdk": "OpenAI-compatible", "env_vars": "(none — local runtime, no key needed)"},
    {"id": "custom", "name": "Others (OpenAI-compatible)", "sdk": "OpenAI-compatible SDK", "env_vars": "LLM_API_KEY + LLM_BASE_URL (required)"},
]

# Fallback `models` text for providers with no _PROVIDER_MODELS entry (custom).
_CUSTOM_MODELS_HINT = "Any model your provider supports"


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
            "thinking_mode": current.get("thinking_mode", "auto") if current else "auto",
            "thinking_capable": _thinking_capable(current["model"]) if current else False,
            "configured": current is not None,
        } if current else None,
        "available": [
            {
                **meta,
                "models": _PROVIDER_MODELS.get(meta["id"], _CUSTOM_MODELS_HINT),
            }
            for meta in _PROVIDER_CATALOG
        ],
    }


@router.get("/pdf-capabilities")
async def pdf_capabilities():
    """Report the available PDF parsing tiers (Fast / Auto / Deep).

    Fast (pdfplumber text layer) is always available. Auto (page-level OCR on
    scanned pages) is available only when the optional OCR group is installed
    via ``./start.sh --ocr``. Deep (Docling) is deferred. The panel uses this to
    show a "PDF parsing: Fast ✓ · Auto ✓/✗" status line and an install hint.
    """
    from pdf_capabilities import get_pdf_capabilities

    caps = get_pdf_capabilities()
    return {
        "fast": caps["fast"],
        "auto": caps["auto"],
        "deep": caps["deep"],
        "ocr_reason": caps.get("ocr_reason"),
        "install_hint": caps.get("install_hint"),
    }


# ---------------------------------------------------------------------------
# Context window tracking
# ---------------------------------------------------------------------------

# Approximate context window sizes per model (in tokens)
MODEL_CONTEXT_WINDOWS = {
    # anthropic — Opus/Sonnet/Fable 5: 1M; Haiku 4.5: 200K
    "claude-opus-5": 1048576,
    "claude-sonnet-5": 1048576,
    "claude-haiku-4-5": 200000,
    "claude-fable-5": 1048576,
    # deepseek — v4 family: 1M
    "deepseek-v4-pro": 1048576,
    "deepseek-v4-flash": 1048576,
    # glm — GLM-5.2: 1M; GLM-5/5.1/4.7/4.6: 200K; 4-Long: 1M; 4.5-Air: 128K
    "glm-5.2": 1048576,
    "glm-5": 200000,
    "glm-5.1": 200000,
    "glm-4.7": 200000,
    "glm-4.6": 200000,
    "glm-4-long": 1048576,
    "glm-4.5-air": 131072,
    # openai — GPT-5 family: 400K; 4.1: 1M; 4o: 128K
    "gpt-5": 400000,
    "gpt-5.1": 400000,
    "gpt-5.2": 400000,
    "gpt-5.3": 400000,
    "gpt-4.1": 1048576,
    "gpt-4o": 128000,
    # gemini — 3.x and 2.5: 1M
    "gemini-3.5-flash": 1048576,
    "gemini-3-pro": 1048576,
    "gemini-3.1-pro": 1048576,
    "gemini-2.5-flash": 1048576,
    "gemini-2.5-pro": 1048576,
    # kimi — K3: 1M; K2.5/2.6/2.7: 256K
    "kimi-k3": 1048576,
    "kimi-k2.7-code": 262144,
    "kimi-k2.6": 262144,
    "kimi-k2.5": 262144,
    # grok — 4.5: 500K; 4.3: 1M; 4.20/4.1-fast: 2M
    "grok-4.5": 524288,
    "grok-4.3": 1048576,
    "grok-4.20": 2097152,
    "grok-4.1-fast": 2097152,
    # minimax — M3: 1M; M2.x: 204,800
    "MiniMax-M3": 1048576,
    "MiniMax-M2.7": 204800,
    "MiniMax-M2.5": 204800,
    "MiniMax-M2.1": 204800,
    # qwen — qwen3-max: 256K; 3.5-plus/plus/flash: 1M
    "qwen3-max": 262144,
    "qwen3.5-plus": 1048576,
    "qwen3.5-flash": 1048576,
    "qwen-plus": 1048576,
    "qwen-flash": 1048576,
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
        # DeepSeek prefix-cache usage from the last request (0 until reported).
        # hit_pct shows how much of the input was served from cache at the
        # cache-hit price — a proxy for how well the prompt ordering works.
        "cache_hit_tokens": _last_cache_hit_tokens,
        "cache_miss_tokens": _last_cache_miss_tokens,
        "cache_hit_pct": round((_last_cache_hit_tokens / (_last_cache_hit_tokens + _last_cache_miss_tokens)) * 100, 1)
        if (_last_cache_hit_tokens + _last_cache_miss_tokens) > 0 else None,
        "manuscript_available": _current_doc is not None,
        "manuscript_total_chars": len(_current_doc.full_text) if _current_doc else 0,
        "scraped_papers_count": len(_scraped_docs),
        "scraped_total_chars": sum(len(doc.full_text) for doc in _scraped_docs),
        "loaded_docs_count": len(_loaded_docs),
        "loaded_docs": [{"name": d["name"], "chars": len(d["text"]), "file_id": d["file_id"]} for d in _loaded_docs],
        "project_docs_count": len(_project_docs),
        "project_docs": [
            {"name": d["name"], "type": d["type"], "chars": len(d["doc"].full_text)}
            for d in _project_docs
        ],
    }


@router.post("/refresh-context")
async def refresh_context():
    """Start a new conversation: condense + clear chat and drop loaded context.

    The button next to the context bar. Frees the context window for a fresh
    line of questioning while keeping the loaded project. This endpoint:

    1. Condenses — runs the LLM digest (flush_memory_if_dirty) so important
       points from the conversation become structured memory (decisions,
       summary, active_context), and records the reviewed file names in
       active_context so the model remembers what was looked at.
    2. Clears the conversation — wipes memory.chat_history (the rolling raw
       turn window) and the pending digest buffer. Distilled memory is kept.
    3. Drops — clears _loaded_docs, _scraped_docs, _scraped_sources, and the
       one-shot _focused_file_cache, plus stale scan-flow flags. The Loaded
       Documents and Scraped Articles panels empty and the window frees up.

    The project baseline (manuscript _current_doc, project docs, comments,
    file index, Drive connection) is left intact. With no project loaded it
    still works — clears any scraped/kept docs and returns a fresh slate.
    """
    global _loaded_docs, _scraped_docs, _scraped_sources, _focused_file_cache
    global _keep_ack, _awaiting_doc_choice, _awaiting_scan_confirmation, _scan_preference
    global _last_request_tokens, _last_request_system_tokens, _last_request_user_tokens

    memory = get_current_memory()

    # Snapshot what's about to be dropped (read under no lock — atomic refs).
    dropped_doc_names = [d["name"] for d in _loaded_docs]
    dropped_scraped_titles = [doc.title or doc.__class__.__name__ for doc in _scraped_docs]
    focused_cleared = len(_focused_file_cache)

    # 1. Condense + reset the conversation. With a loaded project (memory present)
    #    we digest pending exchanges into structured memory first (so important points
    #    survive), then clear the raw rolling chat_history + pending buffer — a true
    #    "new conversation" — while keeping the distilled memory (decisions, summary,
    #    active_context). With no project loaded there's no memory to flush; we just
    #    drop scraped/kept docs below and return a fresh slate. The loaded project
    #    (manuscript, comments, project docs, file index) is left intact either way.
    memory_flushed = False
    chat_turns_cleared = 0
    note = ""
    if memory is not None:
        from memory_manager import flush_memory_if_dirty, reset_pending
        try:
            memory_flushed = await flush_memory_if_dirty()
        except Exception as exc:
            logger.warning("refresh-context: memory digest failed (non-fatal): %s", exc)
        chat_turns_cleared = len(memory.chat_history) // 2
        memory.chat_history = []
        reset_pending()

        # Record what was reviewed in active_context so it survives the drop.
        now = datetime.now(timezone.utc).isoformat()
        note_parts = [f"Context condensed {now[:10]} — conversation reset, dropped from active context:"]
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
        "refresh-context: dropped %d loaded doc(s), %d scraped article(s), %d focused cache entr(ies), %d chat turn(s); memory_flushed=%s",
        len(dropped_doc_names), len(dropped_scraped_titles), focused_cleared, chat_turns_cleared, memory_flushed,
    )

    usage = await context_usage()

    return {
        "status": "refreshed",
        "memory_flushed": memory_flushed,
        "dropped_docs": dropped_doc_names,
        "dropped_docs_count": len(dropped_doc_names),
        "dropped_scraped": dropped_scraped_titles,
        "dropped_scraped_count": len(dropped_scraped_titles),
        "chat_turns_cleared": chat_turns_cleared,
        "project_loaded": _current_doc is not None or get_current_memory() is not None,
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
            "pdf_parse_mode": getattr(_current_doc, "parse_mode", "fast"),
            "ocr_pages": list(getattr(_current_doc, "ocr_pages", [])),
            "ocr_deficient_pages": list(getattr(_current_doc, "ocr_deficient_pages", [])),
            "ocr_deficient_reason": getattr(_current_doc, "ocr_deficient_reason", ""),
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
        "manuscript_file_id": _current_doc_file_id,
        "manuscript_file_name": _current_doc_file_name,
        "comments": comments,
        "images": images,
        "scraped_papers": scraped_papers,
        "loaded_docs": [{"name": d["name"], "chars": len(d["text"]), "file_id": d["file_id"]} for d in _loaded_docs],
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
async def clear_scraped(index: int = None, url: str = None):
    """Clear scraped papers.

    Remove one by ?index=N (matches the order shown in the Loaded Data panel)
    or by ?url=... (stable across reorders, used by the scrape/unload button
    in the tab bar); omit both to clear all.
    """
    global _scraped_docs, _scraped_sources
    if url:
        for i, src in enumerate(_scraped_sources):
            if src == url:
                removed = _scraped_docs.pop(i)
                _scraped_sources.pop(i)
                logger.info("Scraped: removed '%s' by url; %d remaining", removed.title, len(_scraped_docs))
                return {"status": "removed", "title": removed.title, "remaining": len(_scraped_docs)}
        raise HTTPException(status_code=404, detail=f"No scraped paper for url {url}")
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


@router.delete("/loaded-docs")
async def remove_loaded_doc(index: int = None, file_id: str = None):
    """Drop a kept document from the loaded-documents set.

    Remove one by ?index=N (matches the order shown in the Loaded Data panel)
    or by ?file_id=... (stable across reorders, used by the scan/unload button
    in the tab bar); omit both to clear all. The text is only re-injected on
    the next turn, so removing here takes effect immediately for subsequent
    messages.
    """
    global _loaded_docs
    if file_id:
        if _remove_loaded_doc_by_id(file_id):
            return {"status": "removed", "file_id": file_id, "remaining": len(_loaded_docs)}
        raise HTTPException(status_code=404, detail=f"No loaded document with file_id {file_id}")
    if index is not None:
        if 0 <= index < len(_loaded_docs):
            removed = _loaded_docs.pop(index)
            logger.info(
                "Loaded-docs: removed '%s'; %d remaining", removed["name"], len(_loaded_docs)
            )
            return {"status": "removed", "name": removed["name"], "remaining": len(_loaded_docs)}
        raise HTTPException(status_code=404, detail=f"No loaded document at index {index}")
    count = len(_loaded_docs)
    _loaded_docs = []
    logger.info("Loaded-docs: cleared all (%d removed)", count)
    return {"status": "cleared", "removed": count}


async def _refresh_manuscript() -> tuple[list[dict], list[str], list[dict]]:
    """Re-download + re-parse the loaded manuscript and swap ``_current_doc``.

    Text-only re-parse (``figure_ocr=False``), matching how Load Project builds
    ``_current_doc``, so an "Update" picks up Drive edits without the slow
    per-image OCR pass. Returns ``(updated, unchanged, failed)`` in the same
    shape as the kept-doc path. Cache-aware: an unchanged manuscript keeps the
    exact same ``_current_doc`` object, so the injected ## Manuscript block
    stays byte-identical and the provider's prefix cache survives.
    """
    global _current_doc, _focused_file_cache
    name = _current_doc_file_name or (_current_doc.title if _current_doc else "Manuscript")
    old_text = _current_doc.full_text if _current_doc is not None else ""
    try:
        from drive_sync import download_file, _parse_downloaded
        from config import PDF_DEFAULT_MODE

        downloaded = await download_file(_current_doc_file_id)
        file_dict = {"name": name, "mimeType": downloaded.get("mimeType", "")}
        fresh = await asyncio.to_thread(
            _parse_downloaded, file_dict, downloaded,
            pdf_mode=PDF_DEFAULT_MODE, figure_ocr=False,
        )
    except Exception as exc:
        logger.warning("Update-context: manuscript '%s' failed: %s", name, exc)
        return [], [], [{"name": name, "error": str(exc)}]

    if fresh is None or not fresh.full_text:
        return [], [], [{"name": name, "error": "download or parse failed"}]

    if fresh.full_text == old_text:
        logger.info("Update-context: manuscript '%s' unchanged (%d chars) — cache preserved", name, len(fresh.full_text))
        return [], [name], []

    _current_doc = fresh
    # Drop cached one-shot scans of this file so a later focus re-reads fresh.
    _focused_file_cache.pop((_current_doc_file_id, True), None)
    _focused_file_cache.pop((_current_doc_file_id, False), None)
    logger.info(
        "Update-context: refreshed manuscript '%s' (%d -> %d chars)",
        name, len(old_text), len(fresh.full_text),
    )
    return [{"name": name, "chars": len(fresh.full_text)}], [], []


@router.post("/update-context")
async def update_context(file_id: str = None):
    """Re-fetch the open document(s) from Drive and refresh their context text.

    Cache-aware refresh: a document is only replaced when its freshly parsed
    text actually differs from what's kept. An unchanged file keeps the exact
    same bytes, so the stable prompt prefix stays byte-identical and the
    provider's prefix cache keeps serving it at the cache-hit price. Order is
    preserved — a changed document invalidates the cache only from that
    document onward.

    With ``?file_id=...`` the refresh targets one document:
      * the loaded manuscript (``_current_doc_file_id``) — re-parsed text-only,
        matching Load Project;
      * a kept document in ``_loaded_docs`` — re-parsed with figure OCR.
    Omit ``file_id`` to refresh every kept document.
    """
    async with _state_lock:
        updated: list[dict] = []
        unchanged: list[str] = []
        failed: list[dict] = []

        # The manuscript is injected as ## Manuscript from _current_doc (a
        # text-only parse), NOT from _loaded_docs, so it has its own refresh
        # path. When it's also kept, the kept-doc loop below refreshes the
        # OCR'd copy in addition to the text-only _current_doc here.
        is_manuscript = bool(
            file_id and _current_doc is not None and file_id == _current_doc_file_id
        )
        if is_manuscript:
            m_updated, m_unchanged, m_failed = await _refresh_manuscript()
            updated.extend(m_updated)
            unchanged.extend(m_unchanged)
            failed.extend(m_failed)

        if file_id:
            if not is_manuscript and not _is_kept_doc(file_id):
                raise HTTPException(status_code=404, detail=f"No loaded document with file_id {file_id}")
            targets = [d for d in _loaded_docs if d["file_id"] == file_id]
        else:
            targets = list(_loaded_docs)
        if not targets and not is_manuscript:
            return {"updated": [], "unchanged": [], "failed": [], "note": "No kept documents to update"}

        for doc in targets:
            name = doc["name"]
            old_text = doc["text"]
            try:
                # force=True re-downloads from Drive; the parse re-caches the
                # fresh text so future one-shot scans see it too.
                fresh = await _download_and_parse_file(
                    doc["file_id"], name, figure_ocr=True, force=True
                )
            except Exception as exc:
                logger.warning("Update-context: '%s' failed: %s", name, exc)
                failed.append({"name": name, "error": str(exc)})
                continue
            if not fresh:
                failed.append({"name": name, "error": "download or parse failed"})
                continue
            if fresh == old_text:
                unchanged.append(name)
                logger.info("Update-context: '%s' unchanged (%d chars) — cache preserved", name, len(fresh))
            else:
                doc["text"] = fresh
                updated.append({"name": name, "chars": len(fresh)})
                logger.info("Update-context: refreshed '%s' (%d -> %d chars)", name, len(old_text), len(fresh))

    return {"updated": updated, "unchanged": unchanged, "failed": failed}


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
