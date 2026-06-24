"""Memory manager — read/write memory file for cross-session resume."""

from __future__ import annotations

import json
import logging
import os
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


def get_current_memory() -> Optional[RevisionMemory]:
    return _current_memory


def set_current_memory(memory: RevisionMemory) -> None:
    global _current_memory
    _current_memory = memory


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


def update_memory_after_chat(
    user_message: str,
    assistant_message: str,
    updated_comments: list[ReviewerCommentState] = None,
) -> None:
    """Update the in-memory state after a chat turn."""
    memory = get_current_memory()
    if memory is None:
        logger.warning("No active memory to update")
        return

    now = datetime.now(timezone.utc).isoformat()
    hostname = os.uname().nodename if hasattr(os, "uname") else "unknown"

    memory.last_updated = now
    memory.last_computer = hostname

    # Add chat turns
    memory.chat_history.append(
        ChatTurn(role="user", content=user_message[:2000], timestamp=now)
    )
    memory.chat_history.append(
        ChatTurn(role="assistant", content=assistant_message[:2000], timestamp=now)
    )

    # Trim history
    from config import CHAT_HISTORY_LIMIT

    if len(memory.chat_history) > CHAT_HISTORY_LIMIT * 2:
        memory.chat_history = memory.chat_history[-(CHAT_HISTORY_LIMIT * 2):]

    # Update conversation summary (simple: use the user's last message as context)
    memory.conversation_summary = (
        f"Last discussed: {user_message[:300]}\n"
        f"Last response summary: {assistant_message[:300]}"
    )

    # Update reviewer comments if provided
    if updated_comments:
        comment_map = {c.id: c for c in memory.reviewer_comments}
        for updated in updated_comments:
            if updated.id in comment_map:
                # Merge updates
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

    # Save locally
    _save_local(memory)

    # Also sync to Google Drive for cross-computer resume
    if memory.project_folder_id:
        try:
            from drive_sync import _save_memory_to_drive
            _save_memory_to_drive(memory.project_folder_id, memory.model_dump())
            logger.info("Synced memory to Drive folder %s", memory.project_folder_id)
        except Exception as exc:
            logger.warning("Failed to sync memory to Drive (non-fatal): %s", exc)


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
    """Update memory after a chat turn and sync to Google Drive."""
    if get_current_memory() is None:
        raise HTTPException(status_code=400, detail="No active memory. Initialise first.")

    update_memory_after_chat(
        user_message=req.user_message,
        assistant_message=req.assistant_message,
        updated_comments=req.updated_comments,
    )

    # Sync to Google Drive so other computers can resume
    memory = get_current_memory()
    if memory and memory.project_folder_id:
        try:
            from drive_sync import _save_memory_to_drive
            _save_memory_to_drive(memory.project_folder_id, memory.model_dump())
            logger.info("Synced memory to Drive folder %s", memory.project_folder_id)
        except Exception as exc:
            logger.warning("Failed to sync memory to Drive (non-fatal): %s", exc)

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
