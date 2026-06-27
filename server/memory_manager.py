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
