"""Regression tests for the OpenAI-compatible streaming path.

Covers the DeepSeek v4 thinking-mode handling added in 0.1.9: reasoning
models stream chain-of-thought in ``delta.reasoning_content`` while
``delta.content`` stays empty until the answer begins. If a request's output
budget is exhausted by reasoning alone, the stream must surface an explicit
warning instead of silently ending with zero text (the side panel used to be
left with a frozen typing-dots bubble).

The provider stream is stubbed — no network calls are made.
"""

import asyncio
import json

import pytest

import chat_handler
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    Choice,
)


# ---------------------------------------------------------------------------
# Stub OpenAI client
# ---------------------------------------------------------------------------

def _make_chunk(*, reasoning=None, content=None) -> ChatCompletionChunk:
    """Build a real SDK ChatCompletionChunk (also asserts the SDK preserves
    provider-specific ``reasoning_content``)."""
    delta = {"role": "assistant"}
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if content is not None:
        delta["content"] = content
    return ChatCompletionChunk.model_validate(
        {
            "id": "test-chunk",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": None,
                }
            ],
        }
    )


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


@pytest.fixture
def stub_openai(monkeypatch):
    """Replace ``openai.AsyncOpenAI`` with a stub returning recorded chunks.

    Returns a callable that installs the stub for a given chunk list and
    returns the stub client (whose ``completions.kwargs`` records the
    ``create`` call for assertions).
    """
    state = {"client": None}

    def install(chunks):
        state["client"] = _StubClient(chunks)
        monkeypatch.setattr(
            "openai.AsyncOpenAI",
            lambda *a, **k: state["client"],
        )
        return state["client"]

    return install


async def _collect(gen):
    events = []
    async for payload in gen:
        events.append(json.loads(payload[len("data: "):].strip()))
    return events


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_reasoning_only_stream_warns_instead_of_silent_empty(stub_openai):
    """DeepSeek v4 burning its whole output budget reasoning → a 'thinking'
    marker and a 'warning', never a silent empty stream."""
    stub_openai([_make_chunk(reasoning="think think think"), _make_chunk(reasoning="more")])

    async def run():
        return await _collect(
            chat_handler._stream_openai_compatible(
                message="hi", system_prompt="sys", model="deepseek-v4-flash",
                api_key="k", base_url="https://api.deepseek.com",
            )
        )

    events = asyncio.run(run())
    types = [e["type"] for e in events]
    assert types[0] == "thinking"
    assert "warning" in types
    assert not any(t == "text" for t in types)
    assert types[-1] == "done"


def test_reasoning_then_answer_streams_text(stub_openai):
    """Thinking followed by a real answer → thinking marker then text, no
    warning."""
    stub_openai(
        [
            _make_chunk(reasoning="deliberating..."),
            _make_chunk(reasoning="more thinking"),
            _make_chunk(content="Here is the plan."),
        ]
    )

    async def run():
        return await _collect(
            chat_handler._stream_openai_compatible(
                message="hi", system_prompt="sys", model="deepseek-v4-flash",
                api_key="k", base_url="https://api.deepseek.com",
            )
        )

    events = asyncio.run(run())
    types = [e["type"] for e in events]
    assert types[0] == "thinking"
    assert types.count("text") == 1
    assert events[types.index("text")]["content"] == "Here is the plan."
    assert "warning" not in types
    assert types[-1] == "done"


def test_deepseek_v4_gets_larger_output_budget(stub_openai):
    """Thinking mode needs room for reasoning + answer — deepseek-v4 models
    must use a much larger max_tokens than the generic 8192."""
    client = stub_openai([_make_chunk(content="ok")])

    async def run():
        return await _collect(
            chat_handler._stream_openai_compatible(
                message="hi", system_prompt="sys", model="deepseek-v4-flash",
                api_key="k", base_url="https://api.deepseek.com",
            )
        )

    asyncio.run(run())
    assert client.chat.completions.kwargs["max_tokens"] == 32768


def test_non_deepseek_keeps_generic_budget(stub_openai):
    """Non-deepseek providers are untouched: max_tokens stays 8192 and plain
    text streams without any thinking/warning events."""
    client = stub_openai([_make_chunk(content="plain answer")])

    async def run():
        return await _collect(
            chat_handler._stream_openai_compatible(
                message="hi", system_prompt="sys", model="gpt-4o",
                api_key="k", base_url="https://api.openai.com/v1",
            )
        )

    events = asyncio.run(run())
    assert client.chat.completions.kwargs["max_tokens"] == 8192
    assert [e["type"] for e in events] == ["text", "done"]
