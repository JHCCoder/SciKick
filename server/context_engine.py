"""Context engine — chunk paper, map reviewer comments, retrieve for queries."""

import base64
import logging
import re
from collections import Counter
from typing import Optional

from file_processor import (
    FigureInfo,
    PaperDocument,
    ReviewerComment,
    Section,
)

logger = logging.getLogger("paper-assistant.context")


# ---------------------------------------------------------------------------
# Paper context builder
# ---------------------------------------------------------------------------


def build_paper_context(doc: PaperDocument, max_section_chars: int = 8000) -> dict:
    """
    Build a structured context dictionary from a parsed paper.

    Returns a dict with:
    - paper_summary: title + abstract + section headings
    - sections: list of {heading, content (truncated), figures}
    - figures: list of {filename, caption, page, section}
    """
    paper_summary = f"Title: {doc.title}\n"
    if doc.abstract:
        paper_summary += f"Abstract: {doc.abstract[:1500]}\n"
    paper_summary += f"Sections ({len(doc.sections)}): "
    paper_summary += ", ".join(s.heading for s in doc.sections)

    sections = []
    for section in doc.sections:
        sections.append(
            {
                "heading": section.heading,
                "content": section.content[:max_section_chars],
                "truncated": len(section.content) > max_section_chars,
                "figures": section.figures,
            }
        )

    figures = [
        {
            "filename": f.filename,
            "caption": f.caption,
            "page": f.page_number,
            "section": f.section,
        }
        for f in doc.figures
    ]

    return {
        "paper_summary": paper_summary,
        "sections": sections,
        "figures": figures,
        "full_text_length": len(doc.full_text),
    }


def chunk_paper_for_context(doc: PaperDocument, max_chunk_chars: int = 4000) -> list[str]:
    """
    Split the paper into overlapping chunks suitable for retrieval.

    Each chunk tries to stay within section boundaries but splits
    oversized sections further.
    """
    chunks = []
    for section in doc.sections:
        content = section.content
        if len(content) <= max_chunk_chars:
            chunks.append(f"[{section.heading}]\n{content}")
        else:
            # Split long sections into sub-chunks
            paragraphs = content.split("\n\n")
            current_chunk = f"[{section.heading}]\n"
            for para in paragraphs:
                if len(current_chunk) + len(para) > max_chunk_chars and current_chunk.strip():
                    chunks.append(current_chunk.strip())
                    current_chunk = f"[{section.heading} (continued)]\n{para}\n\n"
                else:
                    current_chunk += para + "\n\n"
            if current_chunk.strip():
                chunks.append(current_chunk.strip())
    return chunks


# ---------------------------------------------------------------------------
# Reviewer comment ↔ Paper section mapping
# ---------------------------------------------------------------------------


def map_comments_to_sections(
    comments: list[ReviewerComment],
    doc: PaperDocument,
) -> list[ReviewerComment]:
    """
    Map each reviewer comment to the most relevant paper sections.

    Uses keyword overlap between the comment text and each section.
    Modifies the comment objects in place by setting `related_sections`
    and `related_figures`.
    """
    # Build keyword sets for each section
    section_keywords: dict[str, set[str]] = {}
    for section in doc.sections:
        words = set(
            re.findall(r"\b[a-zA-Z]{4,}\b", section.content.lower())
        )
        section_keywords[section.heading] = words

    figure_names_by_section: dict[str, list[str]] = {}
    for fig in doc.figures:
        if fig.section:
            figure_names_by_section.setdefault(fig.section, []).append(fig.filename)

    for comment in comments:
        comment_words = set(
            re.findall(r"\b[a-zA-Z]{4,}\b", comment.text.lower())
        )

        # Score each section by keyword overlap
        scores: list[tuple[str, float]] = []
        for heading, keywords in section_keywords.items():
            if not keywords:
                continue
            overlap = len(comment_words & keywords)
            score = overlap / min(len(comment_words), len(keywords))
            if score > 0.05:  # low threshold to catch tenuous links
                scores.append((heading, score))

        scores.sort(key=lambda x: x[1], reverse=True)

        # Top 3 related sections
        comment.related_sections = [s[0] for s in scores[:3]]

        # Find related figures via figure captions in comment text
        for fig in doc.figures:
            # Look for "Fig X" or "Figure X" mentions in the comment
            if fig.caption and any(
                word.lower() in comment.text.lower()
                for word in fig.caption.split()[:10]
            ):
                comment.related_figures.append(fig.filename)
            # Also check for explicit figure references
            fig_ref = re.findall(
                r"(?:Fig(?:ure)?\.?\s*\d+[a-zA-Z]?)",
                comment.text,
                re.IGNORECASE,
            )
            for ref in fig_ref:
                if ref.lower() in fig.filename.lower():
                    comment.related_figures.append(fig.filename)

        # Deduplicate
        comment.related_figures = list(set(comment.related_figures))

    return comments


# ---------------------------------------------------------------------------
# Query-time retrieval
# ---------------------------------------------------------------------------


def retrieve_context(
    query: str,
    doc: PaperDocument,
    comments: list[ReviewerComment],
    chat_history: list[dict] = None,
    max_chunks: int = 3,
) -> str:
    """
    Given a user query, return the most relevant context string to inject.

    This is a keyword-based retrieval for simplicity (no embedding needed).
    Returns a formatted string with:
    - The most relevant paper sections
    - The most relevant reviewer comments
    - Recent chat history summary
    """
    query_words = set(re.findall(r"\b[a-zA-Z]{3,}\b", query.lower()))

    # Guard against queries with no extractable keywords (e.g., "Hi", "What?")
    if not query_words:
        # Return a minimal context: just the recent chat history
        parts = []
        if chat_history:
            parts.append("## Recent Conversation\n")
            for turn in chat_history[-6:]:
                role = turn.get("role", "unknown")
                content = turn.get("content", "")[:500]
                parts.append(f"**{role}:** {content}\n")
        return "\n".join(parts)

    # Score paper chunks
    chunks = chunk_paper_for_context(doc)
    chunk_scores = []
    for i, chunk in enumerate(chunks):
        chunk_words = set(re.findall(r"\b[a-zA-Z]{3,}\b", chunk.lower()))
        if not chunk_words:
            continue
        overlap = len(query_words & chunk_words)
        chunk_scores.append((i, overlap / len(query_words), chunk))

    chunk_scores.sort(key=lambda x: x[1], reverse=True)
    top_chunks = chunk_scores[:max_chunks]

    # Score reviewer comments
    comment_scores = []
    for comment in comments:
        comment_words = set(re.findall(r"\b[a-zA-Z]{3,}\b", comment.text.lower()))
        if not comment_words:
            continue
        overlap = len(query_words & comment_words)
        # Boost active/in-progress comments
        boost = 1.5 if hasattr(comment, "status") and getattr(comment, "status", "") == "in_progress" else 1.0
        comment_scores.append((overlap * boost / len(query_words), comment))

    comment_scores.sort(key=lambda x: x[0], reverse=True)
    top_comments = comment_scores[:5]

    # Build context string
    parts = []

    if top_chunks:
        parts.append("## Relevant Paper Sections\n")
        for _, score, chunk in top_chunks[:3]:
            parts.append(chunk[:3000])
            parts.append("\n---\n")

    if top_comments:
        parts.append("## Relevant Reviewer Comments\n")
        for score, comment in top_comments:
            status_str = ""
            if hasattr(comment, "status"):
                status_str = f" [{getattr(comment, 'status', '')}]"
            parts.append(
                f"**{comment.reviewer} ({comment.severity}){status_str}:** {comment.text[:1500]}\n"
            )

    if chat_history:
        parts.append("## Recent Conversation\n")
        for turn in chat_history[-6:]:  # last 3 exchanges
            role = turn.get("role", "unknown")
            content = turn.get("content", "")[:500]
            parts.append(f"**{role}:** {content}\n")

    context = "\n".join(parts)
    logger.info("Retrieved context: %d chunks, %d comments (%d chars)",
                 len(top_chunks), len(top_comments), len(context))

    return context


# ---------------------------------------------------------------------------
# Figure context for vision
# ---------------------------------------------------------------------------


def get_figure_context(
    figure_ref: str,
    doc: PaperDocument,
    image_cache: dict[str, bytes],
) -> Optional[dict]:
    """
    Get the context needed to discuss a specific figure.

    figure_ref can be a filename ("fig2_pca.png") or a reference ("Figure 2",
    "Fig 3a").
    """
    # Find matching figure
    matched_fig: Optional[FigureInfo] = None
    for fig in doc.figures:
        if figure_ref.lower() in fig.filename.lower():
            matched_fig = fig
            break
        if figure_ref.lower() in fig.caption.lower():
            matched_fig = fig
            break

    if matched_fig is None:
        return None

    # Find which section discusses this figure
    related_section = ""
    for section in doc.sections:
        if matched_fig.filename.lower() in section.content.lower():
            related_section = section.heading
            break
        if matched_fig.caption.lower() in section.content.lower():
            related_section = section.heading
            break

    # Get image data if available
    image_data = image_cache.get(matched_fig.filename)
    image_b64 = base64.b64encode(image_data).decode("ascii") if image_data else None

    result = {
        "figure": {
            "filename": matched_fig.filename,
            "caption": matched_fig.caption,
            "page": matched_fig.page_number,
        },
        "related_section": related_section,
        "image_base64": image_b64,
    }

    # Find reviewer comments mentioning this figure
    related_comments = []
    for comment_id, comment_data in image_cache.get("_comments", {}).items():
        # This is populated externally by the API route
        pass

    return result
