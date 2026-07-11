"""Memory manager — read/write memory file for cross-session resume."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import LOCAL_CACHE_DIR, MEMORY_FILE_NAME

logger = logging.getLogger("paper-assistant.memory")
router = APIRouter()

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class PaperSectionSummary(BaseModel):
    hash: str = ""
    summary: str = ""


class ReviewerCommentState(BaseModel):
    id: str
    source: str = ""
    text: str = ""
    severity: str = "unspecified"
    status: str = "pending"  # pending | in_progress | resolved | deferred
    response_draft: str = ""
    related_sections: list[str] = []
    related_figures: list[str] = []
    notes: str = ""
    resolved_at: Optional[str] = None


class Decision(BaseModel):
    date: str
    decision: str


class ChatTurn(BaseModel):
    role: str
    content: str
    timestamp: str = ""


class GoalState(BaseModel):
    """The user's chosen goal/mode for a project, set on first load (or after a
    "change goal" command) and persisted so the AI knows the project's purpose
    on every subsequent load. Mode-specific fields are filled by the onboarding
    Q&A; only the ones relevant to the chosen mode are populated.
    """

    mode: str = ""  # paper_revision | paper_writing | application | grant |
    #               # brainstorming | paper_discussion | other
    created: str = ""
    # Paper modes — target journal + best-effort fetched formatting notes.
    journal: str = ""
    journal_formatting: str = ""  # distilled author-guidelines summary
    journal_lookup_ok: bool = False
    journal_source_url: str = ""  # URL the formatting notes were fetched from
    # Grant mode
    grant_type: str = ""  # NIH | NSF | ERC | foundation | industry | other
    grant_details: str = ""
    # Application mode
    application_type: str = ""  # job | med_school | grad_school | other_professional
    target: str = ""  # school / company / program name
    freeform: str = ""  # extra notes / "other" description
    # Fetched reference info + source URL for application (program) and grant
    # modes (mirrors journal_formatting/journal_source_url for paper modes).
    target_info: str = ""
    target_info_ok: bool = False
    target_info_url: str = ""


class RevisionMemory(BaseModel):
    project_id: str = ""
    project_folder_id: str = ""  # Google Drive folder ID
    project_folder_name: str = ""
    created: str = ""
    last_updated: str = ""
    last_computer: str = ""
    paper_sections: dict[str, PaperSectionSummary] = {}
    reviewer_comments: list[ReviewerCommentState] = []
    response_letter: str = ""
    conversation_summary: str = ""
    decisions: list[Decision] = []
    chat_history: list[ChatTurn] = []
    active_context: str = ""  # what the agent should know on resume
    file_snapshots: dict[str, str] = {}  # file_id -> Drive modifiedTime, for change detection
    goal: Optional[GoalState] = None  # persisted project goal/mode + onboarding answers


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

_current_memory: Optional[RevisionMemory] = None

# Coarse lock guarding multi-step read→await→write critical sections on
# _current_memory and the pending buffer (update_memory_after_chat, digest,
# flush, reset/unload). Single-reference reads/writes via get/set_current_memory
# are atomic in asyncio and need no lock; the lock is for sequences that span
# an await, where another coroutine could mutate mid-flight.
_memory_lock = asyncio.Lock()

# Pending chat exchanges awaiting LLM digest. Appended to in place
# (append/clear) so the module reference stays stable across importers.
_pending_exchanges: list[ChatTurn] = []

# Dirty flag — set whenever memory changes; cleared after a successful Drive sync.
_memory_dirty: bool = False

# Rolling window of recent raw turns kept in chat_history for immediate
# conversational context. Long-term signal lives in the digested structured
# fields (decisions / reviewer_comments / conversation_summary), not here.
RAW_CHAT_WINDOW_TURNS = 6  # turns (user+assistant pairs) → 12 messages


def get_current_memory() -> Optional[RevisionMemory]:
    return _current_memory


def set_current_memory(memory: RevisionMemory) -> None:
    global _current_memory
    _current_memory = memory


def is_memory_dirty() -> bool:
    return _memory_dirty


async def mark_dirty() -> None:
    async with _memory_lock:
        global _memory_dirty
        _memory_dirty = True


async def clear_dirty() -> None:
    async with _memory_lock:
        global _memory_dirty
        _memory_dirty = False


def reset_pending() -> None:
    """Clear the pending digest buffer and dirty flag (used on reset/unload)."""
    global _memory_dirty
    _pending_exchanges.clear()
    _memory_dirty = False


# ---------------------------------------------------------------------------
# Rule-based importance pre-filter (the "C" in the A+C hybrid)
# ---------------------------------------------------------------------------

# Trivial user acknowledgements — never worth buffering/digesting.
_TRIVIAL_ACKS = {
    "ok", "okay", "k", "kk", "thanks", "thank you", "thx", "got it",
    "sure", "yes", "no", "yep", "yup", "nope", "cool", "great", "nice",
    "sounds good", "sounds good!", "will do", "understood", "perfect",
    "awesome", "lol", "haha", "👍", "true",
}

# Keywords that signal an exchange may carry durable signal worth digesting.
_IMPORTANT_TRIGGERS = (
    "remember", "decide", "decision", "important", "note", "let's", "lets",
    "we should", "we need", "we'll", "update", "change", "status", "response",
    "reviewer", "figure", "table", "method", "result", "conclusion", "abstract",
    "rewrite", "revise", "revision", "draft", "address", "fix", "add", "remove",
    "because", "therefore", "however", "should", "need to", "have to",
)

_REVIEWER_ID_RE = re.compile(r"\bR\d+", re.IGNORECASE)


def _is_important_exchange(
    user_message: str,
    assistant_message: str,
    updated_comments: Optional[list[ReviewerCommentState]] = None,
) -> bool:
    """Rule-based pre-filter: True if the exchange may carry durable signal.

    Drops obvious nothing-burgers (trivial acknowledgements) so the LLM digest
    never runs on them. Conservative — when in doubt, keep (digest can still
    decide to extract nothing).
    """
    if updated_comments:
        return True
    u = (user_message or "").strip()
    if u.lower() in _TRIVIAL_ACKS:
        return False
    u_low = u.lower()
    # Very short user message with no trigger, no reviewer id, no question
    # → almost certainly an acknowledgement / filler.
    if (
        len(u_low) < 12
        and not any(t in u_low for t in _IMPORTANT_TRIGGERS)
        and not _REVIEWER_ID_RE.search(u)
        and "?" not in u
    ):
        return False
    return True


def _apply_comment_updates(
    memory: RevisionMemory,
    updated_comments: list[ReviewerCommentState],
    now: str,
) -> None:
    comment_map = {c.id: c for c in memory.reviewer_comments}
    for updated in updated_comments:
        if updated.id in comment_map:
            existing = comment_map[updated.id]
            existing.status = updated.status
            if updated.response_draft:
                existing.response_draft = updated.response_draft
            if updated.notes:
                existing.notes = updated.notes
            if updated.status == "resolved" and not existing.resolved_at:
                existing.resolved_at = now
        else:
            memory.reviewer_comments.append(updated)


# ---------------------------------------------------------------------------
# Local cache helpers
# ---------------------------------------------------------------------------


def _local_cache_path(folder_id: str) -> Path:
    """Get the local cache path for a project folder's memory file."""
    return LOCAL_CACHE_DIR / folder_id / MEMORY_FILE_NAME


def _load_local(folder_id: str) -> Optional[RevisionMemory]:
    """Try to load memory from local cache."""
    cache_file = _local_cache_path(folder_id)
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            return RevisionMemory(**data)
        except Exception as exc:
            logger.warning("Failed to load local cache: %s", exc)
    return None


def _save_local(memory: RevisionMemory) -> None:
    """Save memory to local cache."""
    cache_file = _local_cache_path(memory.project_folder_id)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(memory.model_dump_json(indent=2, exclude_none=True))
    logger.info("Saved memory to local cache: %s", cache_file)


# ---------------------------------------------------------------------------
# Memory operations
# ---------------------------------------------------------------------------


def create_fresh_memory(
    folder_id: str,
    folder_name: str = "",
    project_id: str = "",
) -> RevisionMemory:
    """Create a new empty memory for a project folder."""
    now = datetime.now(timezone.utc).isoformat()
    hostname = os.uname().nodename if hasattr(os, "uname") else "unknown"

    memory = RevisionMemory(
        project_id=project_id or f"revision-{now[:10]}",
        project_folder_id=folder_id,
        project_folder_name=folder_name,
        created=now,
        last_updated=now,
        last_computer=hostname,
    )
    set_current_memory(memory)
    _save_local(memory)
    return memory


async def update_memory_after_chat(
    user_message: str,
    assistant_message: str,
    updated_comments: list[ReviewerCommentState] = None,
) -> None:
    """Buffer a chat exchange for later LLM digest + periodic Drive sync.

    Does NOT upload to Drive — that happens on the periodic sync loop (and on
    flush). The exchange is:
      1. rule-pre-filtered (nothing-burgers skipped entirely),
      2. appended to a pending digest buffer,
      3. mirrored into a short rolling chat_history window for immediate context,
      4. written to the local cache (durable across crashes within the sync
         interval), and the dirty flag is set.
    """
    async with _memory_lock:
        memory = get_current_memory()
        if memory is None:
            logger.warning("No active memory to update")
            return

        if not _is_important_exchange(user_message, assistant_message, updated_comments):
            logger.debug("Skipping nothing-burger exchange (pre-filter)")
            return

        now = datetime.now(timezone.utc).isoformat()
        hostname = os.uname().nodename if hasattr(os, "uname") else "unknown"

        memory.last_updated = now
        memory.last_computer = hostname

        turn_u = ChatTurn(role="user", content=user_message[:2000], timestamp=now)
        turn_a = ChatTurn(role="assistant", content=assistant_message[:2000], timestamp=now)

        # Buffer for the LLM digest
        _pending_exchanges.append(turn_u)
        _pending_exchanges.append(turn_a)

        # Rolling raw window for immediate conversational context
        memory.chat_history.append(turn_u)
        memory.chat_history.append(turn_a)
        if len(memory.chat_history) > RAW_CHAT_WINDOW_TURNS * 2:
            memory.chat_history = memory.chat_history[-(RAW_CHAT_WINDOW_TURNS * 2):]

        # Apply any explicit reviewer-comment updates passed in
        if updated_comments:
            _apply_comment_updates(memory, updated_comments, now)

        global _memory_dirty
        _memory_dirty = True

        # Durable locally immediately (fast filesystem write)
        _save_local(memory)


async def flush_memory() -> None:
    """Digest pending exchanges and upload memory to Drive once. Best-effort.

    Called by the periodic sync loop, on /reset, /unload-project, and shutdown.
    Never raises — a Drive hiccup must not block a reset.

    Only clears the dirty flag if the pending buffer is fully drained. If the
    digest failed (LLM/parse error) and pending is retained, dirty stays set
    so the periodic loop retries — no orphaned, never-digested exchanges.
    """
    # Digest first (releases the lock during the LLM call).
    try:
        from memory_digest import digest_pending_exchanges

        await digest_pending_exchanges()
    except Exception as exc:
        logger.warning("flush_memory: digest failed (non-fatal): %s", exc)

    memory = get_current_memory()
    if memory is not None and memory.project_folder_id:
        try:
            from drive_sync import _save_memory_to_drive

            await _save_memory_to_drive(memory.project_folder_id, memory.model_dump())
            logger.info("Memory synced to Drive (flush)")
        except Exception as exc:
            logger.warning("flush_memory: Drive sync failed (non-fatal): %s", exc)

    # Clear dirty only when pending is drained; otherwise keep it set so the
    # loop retries the digest.
    if len(_pending_exchanges) == 0:
        await clear_dirty()


async def flush_memory_if_dirty() -> bool:
    """Flush (digest + sync) only if there's unsaved work.

    Returns True if a flush ran, False if there was nothing to save (buffer
    clean → caller can restart instantly with no Drive call). Used by Restart
    so an empty buffer doesn't pay a Drive round-trip.
    """
    if not is_memory_dirty():
        return False
    await flush_memory()
    return True


def update_paper_sections(sections: list[dict]) -> None:
    """Update paper section summaries in memory."""
    import hashlib

    memory = get_current_memory()
    if memory is None:
        return

    memory.paper_sections = {}
    for section in sections:
        heading = section.get("heading", "Unknown")
        content_hash = hashlib.md5(
            section.get("content", "").encode()
        ).hexdigest()[:12]
        memory.paper_sections[heading] = PaperSectionSummary(
            hash=content_hash,
            summary=section.get("content", "")[:500],
        )

    _save_local(memory)


_GOAL_MODE_LABELS = {
    "paper_revision": "Paper revision",
    "paper_writing": "Paper writing",
    "application": "Application",
    "grant": "Grant",
    "brainstorming": "Brainstorming",
    "paper_discussion": "Paper discussion",
    "other": "Other",
}

_GRANT_TYPE_LABELS = {
    "NIH": "NIH", "NSF": "NSF", "ERC": "ERC",
    "foundation": "Foundation", "industry": "Industry", "other": "Other",
}

_APP_TYPE_LABELS = {
    "job": "Job application",
    "med_school": "Medical school",
    "grad_school": "Grad school / PhD",
    "other_professional": "Other professional application",
}


def goal_block(goal: "GoalState") -> str:
    """Render a GoalState as a markdown block for the system prompt / resume
    context. Shows the mode and every populated mode-specific field, plus a
    directive to tailor all advice to the goal. Used both in the system prompt
    (every turn) and in the resume context, so the rendering stays consistent.
    """
    label = _GOAL_MODE_LABELS.get(goal.mode, goal.mode or "Unspecified")
    lines = [f"## Project Goal", f"Mode: **{label}**"]

    if goal.mode in ("paper_revision", "paper_writing"):
        if goal.journal:
            lines.append(f"Target journal: **{goal.journal}**")
        if goal.journal_formatting:
            lines.append("")
            lines.append("Author-guidelines notes (formatting):")
            lines.append(goal.journal_formatting)
        directive = (
            f"Tailor all revision/writing advice to {goal.journal or 'the target journal'}'s "
            "formatting conventions. If the notes above are empty or you are unsure of a "
            "specific rule, rely on your general knowledge of that journal's style, and ask "
            "the user to paste the author guidelines if precision matters."
        )
    elif goal.mode == "grant":
        if goal.grant_type:
            lines.append(
                f"Grant type: **{_GRANT_TYPE_LABELS.get(goal.grant_type, goal.grant_type)}**"
            )
        if goal.grant_details:
            lines.append(f"Grant details: {goal.grant_details}")
        if goal.target_info:
            lines.append("")
            lines.append("Grant-program notes (looked up):")
            lines.append(goal.target_info)
        directive = (
            "Tailor all advice to this grant's aims, review criteria, and structure "
            "(specific aims, significance, innovation, approach, etc. as applicable)."
        )
    elif goal.mode == "application":
        if goal.application_type:
            lines.append(
                f"Application type: **{_APP_TYPE_LABELS.get(goal.application_type, goal.application_type)}**"
            )
        if goal.target:
            lines.append(f"Target: **{goal.target}**")
        if goal.freeform:
            lines.append(f"Notes: {goal.freeform}")
        if goal.target_info:
            lines.append("")
            lines.append("Program/target notes (looked up):")
            lines.append(goal.target_info)
        directive = (
            "Tailor all advice to this application's expectations and audience "
            "(statement of purpose, activities, fit with the target, etc.)."
        )
    else:
        if goal.freeform:
            lines.append(f"Notes: {goal.freeform}")
        directive = "Follow the user's lead on what they need."

    lines.append("")
    lines.append(directive)
    return "\n".join(lines) + "\n"


def goal_payload() -> Optional[dict]:
    """Return the current saved goal as a dict for API responses, or None.

    Includes a prebuilt ``recap`` string so the side panel can display the
    goal summary without re-deriving it client-side.
    """
    memory = get_current_memory()
    if not (memory and memory.goal and memory.goal.mode):
        return None
    g = memory.goal
    return {**g.model_dump(), "recap": goal_recap_text(g)}


def goal_recap_text(goal: "GoalState") -> str:
    """Short user-facing recap of the saved goal, shown at finalize time and on
    subsequent project loads. Ends with the "say change goal" correction hint.
    """
    label = _GOAL_MODE_LABELS.get(goal.mode, goal.mode or "Unspecified")
    bits = [f"**Goal:** {label}"]
    if goal.mode in ("paper_revision", "paper_writing"):
        if goal.journal:
            bits.append(f"targeting **{goal.journal}**")
        if goal.journal_lookup_ok and goal.journal_source_url:
            bits.append(
                f"— I inserted a rough journal-specific formatting guideline "
                f"from my training knowledge of {goal.journal} into context. "
                f"However, this is not a live read of its guidelines, so if you "
                f"need the exact/detailed rule, find the specific guide in a new "
                f"tab and scrape the real guidelines into context "
                f"(Good place to start: {goal.journal_source_url})"
            )
        elif goal.journal_lookup_ok and goal.journal_formatting:
            bits.append(
                "— these formatting notes come from my training knowledge of "
                "this journal, not a live read of its guidelines; for exact "
                "rules, scrape its author-guidelines page into context"
            )
        elif goal.journal:
            bits.append(
                "— I couldn't look up specific formatting notes, so I'll use "
                "my general training knowledge of this journal's style"
            )
    elif goal.mode == "grant":
        if goal.grant_type:
            bits.append(f"{_GRANT_TYPE_LABELS.get(goal.grant_type, goal.grant_type)} grant")
        if goal.grant_details:
            bits.append("— specifics noted")
        if goal.target_info_ok and goal.target_info_url:
            bits.append(
                f"— grant-program info fetched from {goal.target_info_url}"
            )
        elif goal.target_info_ok and goal.target_info:
            bits.append("— grant-program info loaded")
    elif goal.mode == "application":
        if goal.application_type:
            bits.append(_APP_TYPE_LABELS.get(goal.application_type, goal.application_type))
        if goal.target:
            bits.append(f"→ **{goal.target}**")
        if goal.freeform:
            bits.append(f"({goal.freeform})")
        if goal.target_info_ok and goal.target_info_url:
            bits.append(
                f"— program info fetched from {goal.target_info_url}"
            )
        elif goal.target_info_ok and goal.target_info:
            bits.append("— program info loaded")
    elif goal.freeform:
        bits.append(goal.freeform)

    return (
        "Got it — " + " ".join(bits) + ". "
        "I'll keep this in mind for every conversation on this project. "
        'To change the goal/purpose associated with this folder, just say **"change goal"**.'
    )


def build_resume_context() -> str:
    """Build a context string for the Claude system prompt on resume."""
    memory = get_current_memory()
    if memory is None:
        return ""

    parts = []
    parts.append(
        f"## Resumed Session\n"
        f"Project: {memory.project_folder_name}\n"
        f"Last active: {memory.last_updated} on {memory.last_computer}\n"
    )

    if memory.goal and memory.goal.mode:
        parts.append(goal_block(memory.goal))

    if memory.conversation_summary:
        parts.append(f"\n### Previous Context\n{memory.conversation_summary}\n")

    # Summarise reviewer comment status
    status_counts = {"pending": 0, "in_progress": 0, "resolved": 0, "deferred": 0}
    for c in memory.reviewer_comments:
        status_counts[c.status] = status_counts.get(c.status, 0) + 1

    total = len(memory.reviewer_comments)
    if total > 0:
        parts.append(
            f"\n### Reviewer Comment Status\n"
            f"Total: {total} | "
            f"Resolved: {status_counts['resolved']} | "
            f"In Progress: {status_counts['in_progress']} | "
            f"Pending: {status_counts['pending']}\n"
        )

        # List in-progress comments
        in_progress = [c for c in memory.reviewer_comments if c.status == "in_progress"]
        if in_progress:
            parts.append("\n**Currently in progress:**\n")
            for c in in_progress[:5]:
                parts.append(f"- {c.id}: {c.text[:200]}...\n")

    if memory.decisions:
        parts.append("\n### Key Decisions\n")
        for d in memory.decisions[-10:]:
            parts.append(f"- [{d.date}] {d.decision}\n")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------


class MemoryInitRequest(BaseModel):
    folder_id: str
    folder_name: str = ""
    project_id: str = ""


class MemoryUpdateRequest(BaseModel):
    user_message: str
    assistant_message: str = ""
    updated_comments: list[ReviewerCommentState] = []


class PaperSectionsRequest(BaseModel):
    sections: list[dict] = []


@router.post("/init")
async def init_memory(req: MemoryInitRequest):
    """Initialise a new memory for a project folder."""
    memory = create_fresh_memory(
        folder_id=req.folder_id,
        folder_name=req.folder_name,
        project_id=req.project_id,
    )
    return {"status": "initialised", "memory": memory.model_dump()}


@router.get("/status")
async def memory_status():
    """Get current memory status."""
    memory = get_current_memory()
    if memory is None:
        return {"active": False, "memory": None}

    status_counts = {"pending": 0, "in_progress": 0, "resolved": 0, "deferred": 0}
    for c in memory.reviewer_comments:
        status_counts[c.status] = status_counts.get(c.status, 0) + 1

    return {
        "active": True,
        "memory": memory.model_dump(),
        "summary": {
            "total_comments": len(memory.reviewer_comments),
            "resolved": status_counts["resolved"],
            "in_progress": status_counts["in_progress"],
            "pending": status_counts["pending"],
            "chat_turns": len(memory.chat_history) // 2,
        },
    }


@router.post("/update")
async def update_memory(req: MemoryUpdateRequest):
    """Update memory after a chat turn and sync to Google Drive.

    The Drive sync runs in a thread pool so the event loop stays responsive
    for health checks while the upload is in progress.
    """
    if get_current_memory() is None:
        return {"status": "skipped", "reason": "No active memory — load a project to persist chat history."}

    await update_memory_after_chat(
        user_message=req.user_message,
        assistant_message=req.assistant_message,
        updated_comments=req.updated_comments,
    )

    return {"status": "updated"}


@router.post("/sections")
async def update_sections(req: PaperSectionsRequest):
    """Update paper section summaries in memory."""
    if get_current_memory() is None:
        raise HTTPException(status_code=400, detail="No active memory.")
    update_paper_sections(req.sections)
    return {"status": "sections_updated"}


@router.post("/decision")
async def add_decision(decision: str):
    """Record a decision made during revision."""
    memory = get_current_memory()
    if memory is None:
        raise HTTPException(status_code=400, detail="No active memory.")

    now = datetime.now(timezone.utc).isoformat()
    memory.decisions.append(Decision(date=now, decision=decision[:500]))
    _save_local(memory)
    return {"status": "decision_recorded"}


@router.put("/comment/{comment_id}")
async def update_comment(comment_id: str, update: ReviewerCommentState):
    """Update a single reviewer comment's state."""
    memory = get_current_memory()
    if memory is None:
        raise HTTPException(status_code=400, detail="No active memory.")

    for i, comment in enumerate(memory.reviewer_comments):
        if comment.id == comment_id:
            memory.reviewer_comments[i] = update
            _save_local(memory)
            return {"status": "updated", "comment": update.model_dump()}

    raise HTTPException(status_code=404, detail=f"Comment {comment_id} not found")
