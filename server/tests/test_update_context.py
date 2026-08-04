"""Tests for the cache-aware update-context endpoint (0.1.9).

POST /chat/update-context re-fetches kept document(s) from Drive and replaces
their context text ONLY when the freshly parsed bytes actually differ. An
unchanged file keeps the exact same bytes, so the stable prompt prefix stays
byte-identical and the provider's prefix cache keeps serving it — that's the
"clever cache" contract. Drive download/parse is stubbed; no network calls.
"""

import asyncio

import pytest
from fastapi import HTTPException

import chat_handler


# ---------------------------------------------------------------------------
# State fixture — save/restore module globals the endpoint reads/mutates
# ---------------------------------------------------------------------------

_STATE_KEYS = ["_loaded_docs", "_focused_file_cache"]


@pytest.fixture(autouse=True)
def _clean_state():
    saved = {k: getattr(chat_handler, k) for k in _STATE_KEYS}
    chat_handler._loaded_docs = []
    chat_handler._focused_file_cache = {}
    yield
    for k, v in saved.items():
        setattr(chat_handler, k, v)


# ---------------------------------------------------------------------------
# Stub downloader
# ---------------------------------------------------------------------------

def _install_stub_downloader(monkeypatch, text_by_id, seen):
    """Replace _download_and_parse_file with a stub returning text_by_id[file_id]
    (or None for unknown ids). Records (file_id, force) into `seen` so tests can
    assert the endpoint always requests a fresh, force=True parse. Mimics the
    real function's side effect: a successful parse is re-cached, so a later
    one-shot scan sees the fresh text."""
    async def fake(file_id, file_name, figure_ocr=True, force=False):
        seen.append((file_id, force))
        text = text_by_id.get(file_id)
        if text:
            chat_handler._focused_file_cache[(file_id, figure_ocr)] = text
        return text

    monkeypatch.setattr(chat_handler, "_download_and_parse_file", fake)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_no_kept_docs_returns_note(monkeypatch):
    _install_stub_downloader(monkeypatch, {}, [])

    async def run():
        return await chat_handler.update_context()

    res = asyncio.run(run())
    assert res["note"] == "No kept documents to update"
    assert res["updated"] == [] and res["unchanged"] == [] and res["failed"] == []


def test_unchanged_doc_keeps_bytes_cache_preserved(monkeypatch):
    """The core cache contract: an unchanged file keeps its exact stored text —
    no replacement, so the prompt prefix stays byte-identical."""
    chat_handler._loaded_docs = [
        {"file_id": "f1", "name": "Supp.docx", "text": "UNCHANGED BODY"}
    ]
    seen = []
    _install_stub_downloader(monkeypatch, {"f1": "UNCHANGED BODY"}, seen)

    async def run():
        return await chat_handler.update_context()

    res = asyncio.run(run())
    assert res["unchanged"] == ["Supp.docx"]
    assert res["updated"] == []
    assert chat_handler._loaded_docs[0]["text"] == "UNCHANGED BODY"
    assert seen == [("f1", True)]  # forced a fresh download, no cache shortcut


def test_changed_doc_replaces_text_and_refreshes_cache(monkeypatch):
    chat_handler._loaded_docs = [
        {"file_id": "f1", "name": "Supp.docx", "text": "OLD BODY"}
    ]
    seen = []
    _install_stub_downloader(monkeypatch, {"f1": "NEW BODY FROM DRIVE"}, seen)

    async def run():
        return await chat_handler.update_context()

    res = asyncio.run(run())
    assert res["updated"] == [{"name": "Supp.docx", "chars": len("NEW BODY FROM DRIVE")}]
    assert res["unchanged"] == []
    assert chat_handler._loaded_docs[0]["text"] == "NEW BODY FROM DRIVE"
    assert seen == [("f1", True)]
    # The focused-file cache reflects the fresh text too, so a later one-shot
    # scan of this file doesn't resurrect the stale version.
    assert chat_handler._focused_file_cache[("f1", True)] == "NEW BODY FROM DRIVE"


def test_update_by_file_id_only_touches_that_doc(monkeypatch):
    chat_handler._loaded_docs = [
        {"file_id": "f1", "name": "A.docx", "text": "A OLD"},
        {"file_id": "f2", "name": "B.docx", "text": "B OLD"},
    ]
    seen = []
    _install_stub_downloader(monkeypatch, {"f1": "A NEW", "f2": "B OLD"}, seen)

    async def run():
        return await chat_handler.update_context(file_id="f1")

    res = asyncio.run(run())
    assert [u["name"] for u in res["updated"]] == ["A.docx"]
    assert chat_handler._loaded_docs[0]["text"] == "A NEW"
    assert chat_handler._loaded_docs[1]["text"] == "B OLD"  # untouched
    assert seen == [("f1", True)]  # only the targeted file was downloaded


def test_unknown_file_id_raises_404(monkeypatch):
    _install_stub_downloader(monkeypatch, {}, [])

    async def run():
        return await chat_handler.update_context(file_id="ghost")

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(run())
    assert excinfo.value.status_code == 404


def test_download_failure_reported_not_crashing(monkeypatch):
    chat_handler._loaded_docs = [
        {"file_id": "f1", "name": "Broken.docx", "text": "OLD"}
    ]
    seen = []
    _install_stub_downloader(monkeypatch, {"f1": None}, seen)  # download/parse failed

    async def run():
        return await chat_handler.update_context()

    res = asyncio.run(run())
    assert len(res["failed"]) == 1
    assert res["failed"][0]["name"] == "Broken.docx"
    assert chat_handler._loaded_docs[0]["text"] == "OLD"  # unchanged on failure
