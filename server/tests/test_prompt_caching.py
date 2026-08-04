"""Regression tests for DeepSeek context-cache optimization (0.1.9).

DeepSeek's prefix cache serves identical input prefixes at ~1/50th the input
price, but only when consecutive requests share a byte-identical prefix from
token 0. The prompt builders are therefore structured so the stable document
context is the prefix and every per-turn changing block sits in the tail:

  - the system prompt is byte-stable (no per-turn "Context Window Status"
    numbers, no digest-changing resume block),
  - the user message puts the stable Loaded Documents / Web-Scraped Papers
    blocks BEFORE the per-turn retrieved context and the user message,
  - streaming requests set ``stream_options.include_usage`` (deepseek-v4 only)
    so the cache hit/miss token counts can be read back and logged.

No network calls are made — the provider stream is stubbed.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

import chat_handler


# ---------------------------------------------------------------------------
# Stub OpenAI client (same shape as test_chat_streaming.py)
# ---------------------------------------------------------------------------

class _AsyncIter:
    def __init__(self, items):
        self._it = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _StubCompletions:
    def __init__(self, chunks):
        self._chunks = chunks
        self.kwargs = None  # captures the create() call for assertions

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return _AsyncIter(self._chunks)


class _StubChat:
    def __init__(self, chunks):
        self.completions = _StubCompletions(chunks)


class _StubClient:
    def __init__(self, chunks):
        self.chat = _StubChat(chunks)


def _fake_chunk(content=None, reasoning=None, usage=None):
    delta = SimpleNamespace(content=content, reasoning_content=reasoning)
    return SimpleNamespace(
        usage=usage,
        choices=[SimpleNamespace(delta=delta, index=0, finish_reason=None)],
    )


async def _collect(gen):
    events = []
    async for payload in gen:
        events.append(payload)
    return events


# ---------------------------------------------------------------------------
# State fixture — save/restore the module globals the prompt builders read
# ---------------------------------------------------------------------------

_STATE_KEYS = [
    "_loaded_docs", "_current_doc", "_current_doc_file_id", "_current_doc_file_name",
    "_project_docs", "_project_summary", "_current_comments", "_image_cache",
    "_keep_ack", "_scraped_docs", "_scraped_sources",
    "_last_cache_hit_tokens", "_last_cache_miss_tokens",
]


@pytest.fixture(autouse=True)
def _clean_state():
    saved = {k: getattr(chat_handler, k) for k in _STATE_KEYS}
    # Defaults that make the prompt builders inert.
    chat_handler._loaded_docs = []
    chat_handler._current_doc = None
    chat_handler._current_doc_file_id = ""
    chat_handler._current_doc_file_name = ""
    chat_handler._project_docs = []
    chat_handler._project_summary = ""
    chat_handler._current_comments = []
    chat_handler._image_cache = {}
    chat_handler._keep_ack = None
    chat_handler._scraped_docs = []
    chat_handler._scraped_sources = []
    chat_handler._last_cache_hit_tokens = 0
    chat_handler._last_cache_miss_tokens = 0
    yield
    for k, v in saved.items():
        setattr(chat_handler, k, v)


# ---------------------------------------------------------------------------
# System prompt stability
# ---------------------------------------------------------------------------

def test_system_prompt_has_no_per_turn_blocks():
    """The system prompt must be byte-stable across turns — no context-status
    numbers (change every turn) and no resume block (changes on digest)."""
    sp = chat_handler._build_system_prompt()
    assert "## Context Window Status" not in sp
    assert "In use:" not in sp
    assert "## Session Resumed" not in sp


def test_context_status_block_exists_and_lives_in_tail():
    """The per-turn guidance still exists — just detached from the system
    prompt so it can be appended at the end of the user message."""
    block = chat_handler._context_status_block()
    assert "## Context Window Status" in block
    assert "Window:" in block


# ---------------------------------------------------------------------------
# User message ordering
# ---------------------------------------------------------------------------

def test_stable_docs_precede_retrieved_context_and_question(monkeypatch):
    """Loaded Documents + Scraped Papers must come before the per-turn
    retrieved context and the user message, so the stable document text is a
    cacheable prefix; the changing Context Window Status stays at the tail."""
    chat_handler._loaded_docs = [
        {"name": "Supplement.docx", "text": "SUPPLEMENT BODY " * 50, "file_id": "s1"}
    ]
    chat_handler._current_doc = SimpleNamespace()  # enough to enable retrieval
    chat_handler._scraped_docs = []
    monkeypatch.setattr(
        chat_handler, "retrieve_context",
        lambda **kw: "## RETRIEVAL_MARKER\nper-turn retrieval output",
    )

    msg = chat_handler._build_user_message(
        message="hello",
        include_paper=True,
        include_comments=True,
        current_file=None,
        session_focus=None,
    )

    i_loaded = msg.find("## Loaded Documents")
    i_retrieval = msg.find("## RETRIEVAL_MARKER")
    i_user = msg.find("## User Message")
    i_status = msg.find("## Context Window Status")
    assert -1 not in (i_loaded, i_retrieval, i_user, i_status)
    assert i_loaded < i_retrieval < i_user, (
        "stable docs must precede the changing retrieval and the question"
    )
    assert i_status > i_user, "context-status guidance must stay at the tail"


# ---------------------------------------------------------------------------
# include_usage + cache-hit capture
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider,model,base_url", [
    ("deepseek", "deepseek-v4-flash", "https://api.deepseek.com"),
    ("openai", "gpt-4o", "https://api.openai.com/v1"),
    ("gemini", "gemini-2.5-flash", "https://generativelanguage.googleapis.com/v1beta/openai"),
])
def test_stream_requests_usage_for_caching_providers(monkeypatch, provider, model, base_url):
    """DeepSeek / OpenAI / Gemini all accept stream_options.include_usage and
    report cache token counts — request it so hits can be read back."""
    client = _StubClient([_fake_chunk(content="hi")])
    monkeypatch.setattr("openai.AsyncOpenAI", lambda *a, **k: client)

    async def run():
        await _collect(
            chat_handler._stream_openai_compatible(
                "hi", "sys", model, "k", base_url, provider=provider
            )
        )

    asyncio.run(run())
    assert client.chat.completions.kwargs["stream_options"] == {"include_usage": True}


def test_stream_no_usage_field_for_other_providers(monkeypatch):
    """Providers without documented include_usage support (glm, kimi, custom,
    local) get no stream_options field at all — they may reject it."""
    client = _StubClient([_fake_chunk(content="hi")])
    monkeypatch.setattr("openai.AsyncOpenAI", lambda *a, **k: client)

    async def run():
        await _collect(
            chat_handler._stream_openai_compatible(
                "hi", "sys", "glm-4-flash", "k", "https://open.bigmodel.cn/api/paas/v4",
                provider="glm",
            )
        )

    asyncio.run(run())
    assert "stream_options" not in client.chat.completions.kwargs


def test_stream_captures_deepseek_cache_hit_miss_tokens(monkeypatch):
    usage = SimpleNamespace(prompt_cache_hit_tokens=900, prompt_cache_miss_tokens=100)
    client = _StubClient(
        [_fake_chunk(content="answer"), _fake_chunk(usage=usage)]
    )
    monkeypatch.setattr("openai.AsyncOpenAI", lambda *a, **k: client)

    async def run():
        await _collect(
            chat_handler._stream_openai_compatible(
                "hi", "sys", "deepseek-v4-flash", "k", "https://api.deepseek.com",
                provider="deepseek",
            )
        )

    asyncio.run(run())
    assert chat_handler._last_cache_hit_tokens == 900
    assert chat_handler._last_cache_miss_tokens == 100


def test_openai_cached_tokens_parsed_from_details():
    """OpenAI reports cached tokens under prompt_tokens_details.cached_tokens;
    the miss share is prompt_tokens minus the cached subset."""
    usage = SimpleNamespace(
        prompt_tokens=1200,
        prompt_tokens_details=SimpleNamespace(cached_tokens=900),
    )
    chat_handler._record_cache_usage("gpt-4o", usage, provider="openai")
    assert chat_handler._last_cache_hit_tokens == 900
    assert chat_handler._last_cache_miss_tokens == 300


def test_gemini_cached_content_count_fallback():
    """Gemini's native usage reports cached content under
    cachedContentTokenCount (OpenAI-compat may or may not surface it)."""
    usage = SimpleNamespace(cachedContentTokenCount=500)
    chat_handler._record_cache_usage("gemini-2.5-flash", usage, provider="gemini")
    assert chat_handler._last_cache_hit_tokens == 500


def test_record_cache_usage_updates_meter_and_logs(caplog):
    caplog.set_level(logging.INFO, logger="paper-assistant.chat")
    chat_handler._record_cache_usage(
        "deepseek-v4-flash",
        SimpleNamespace(prompt_cache_hit_tokens=750, prompt_cache_miss_tokens=250),
        provider="deepseek",
    )
    assert chat_handler._last_cache_hit_tokens == 750
    assert chat_handler._last_cache_miss_tokens == 250
    assert "75.0% cached" in caplog.text


def test_record_cache_usage_none_is_noop():
    chat_handler._last_cache_hit_tokens = 42
    chat_handler._record_cache_usage("gpt-4o", None)
    assert chat_handler._last_cache_hit_tokens == 42
