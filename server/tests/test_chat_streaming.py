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


def test_glm_thinking_family_gets_budget_and_off_param(stub_openai):
    """glm-4.7 (a reasoning family) gets the larger output budget while
    thinking, and the family's 'off' body when thinking is disabled."""
    client = stub_openai([_make_chunk(content="ok")])

    async def run(thinking):
        return await _collect(
            chat_handler._stream_openai_compatible(
                message="hi", system_prompt="sys", model="glm-4.7",
                api_key="k", base_url="https://api.bigmodel.cn",
                thinking=thinking,
            )
        )

    asyncio.run(run(thinking=False))
    assert client.chat.completions.kwargs["max_tokens"] == 8192
    assert client.chat.completions.kwargs["extra_body"] == {
        "thinking": {"type": "disabled"}
    }

    asyncio.run(run(thinking=True))
    assert client.chat.completions.kwargs["max_tokens"] == 32768
    assert "extra_body" not in client.chat.completions.kwargs


def test_qwen_off_param(stub_openai):
    """qwen toggles thinking off via enable_thinking: false, and keeps the
    generic 8192 output budget (its CoT doesn't need the boost)."""
    client = stub_openai([_make_chunk(content="ok")])

    async def run():
        return await _collect(
            chat_handler._stream_openai_compatible(
                message="hi", system_prompt="sys", model="qwen3-max",
                api_key="k", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                thinking=False,
            )
        )

    asyncio.run(run())
    assert client.chat.completions.kwargs["max_tokens"] == 8192
    assert client.chat.completions.kwargs["extra_body"] == {"enable_thinking": False}


def test_sync_openai_compatible_sends_off_param(monkeypatch):
    """The non-streaming path applies the same family lookup for 'off'."""
    calls = {}

    class _Msg:
        content = "ok"

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]
        usage = None

    class _Completions:
        async def create(self, **kwargs):
            calls["kwargs"] = kwargs
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    monkeypatch.setattr("openai.AsyncOpenAI", lambda *a, **k: _Client())

    async def run():
        return await chat_handler._sync_openai_compatible(
            message="hi", system_prompt="sys", model="glm-4.7",
            api_key="k", base_url="https://api.bigmodel.cn",
            thinking=False,
        )

    assert asyncio.run(run()) == "ok"
    assert calls["kwargs"]["max_tokens"] == 8192
    assert calls["kwargs"]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_capability_map_buckets_models():
    """Always-on / never-off models aren't toggle-capable; reasoning models are.

    Gemini is excluded entirely — its OpenAI-compat endpoint rejects the
    thinkingBudget field, so thinking can't be toggled per-request.
    """
    for model in ("gpt-4.1", "gpt-4o", "glm-4-long", "kimi-k3", "kimi-k2.7-code",
                  "gemini-3.5-flash", "gemini-2.5-pro", "gemini-2.5-flash",
                  "MiniMax-M2.5", "grok-4.5", "grok-4.20", "claude-fable-5", "gpt-5"):
        assert not chat_handler._thinking_capable(model), model
    for model in ("deepseek-v4-pro", "qwen3.5-plus", "glm-4.7", "kimi-k2.6",
                  "gpt-5.2", "MiniMax-M3", "grok-4.3"):
        assert chat_handler._thinking_capable(model), model


def test_think_stripper_handles_chunk_boundaries():
    """A <think> block split across stream chunks is buffered and dropped whole."""
    st = chat_handler._ThinkStripper()
    pieces, entered = st.process("<think>first half ")
    assert pieces == [] and entered is True
    pieces, entered = st.process("second half</think>answer")
    assert pieces == ["answer"] and entered is False
    assert st.flush() == ""


def test_think_stripper_multiple_blocks_plain_text_and_split_tag():
    st = chat_handler._ThinkStripper()
    pieces, entered = st.process("<think>a</think>one <think>b</think>two")
    assert pieces == ["one ", "two"] and entered is True

    st2 = chat_handler._ThinkStripper()
    assert st2.process("plain text") == (["plain text"], False)

    # The open tag itself can be split mid-word across chunks.
    st3 = chat_handler._ThinkStripper()
    assert st3.process("<thi") == ([], False)
    assert st3.process("nk>hidden</think>out") == (["out"], True)


def test_think_stripper_drops_unclosed_block():
    """The model ran out mid-thought — the trailing CoT is dropped, not shown."""
    st = chat_handler._ThinkStripper()
    st.process("<think>never closed")
    assert st.flush() == ""


def test_strip_think_tags_removes_blocks_from_full_reply():
    assert chat_handler._strip_think_tags("<think>CoT here</think>Final answer") == "Final answer"
    assert chat_handler._strip_think_tags("no tags here") == "no tags here"
    assert (chat_handler._strip_think_tags("before<think>a</think>mid<think>b</think>after")
            == "beforemidafter")


def test_minimax_think_tags_stripped_from_stream(stub_openai):
    """MiniMax-M3 streams CoT as literal <think> text — stripped to just the
    answer, with a one-shot thinking marker while the block buffers."""
    client = stub_openai([
        _make_chunk(content="<think>"),
        _make_chunk(content="deliberating about the design..."),
        _make_chunk(content="</think>Here is the answer."),
    ])

    async def run():
        return await _collect(
            chat_handler._stream_openai_compatible(
                message="hi", system_prompt="sys", model="MiniMax-M3",
                api_key="k", base_url="https://api.minimax.io/v1",
                provider="minimax", thinking=True,
            )
        )

    events = asyncio.run(run())
    types = [e["type"] for e in events]
    assert types[0] == "thinking"
    texts = "".join(e.get("content", "") for e in events if e["type"] == "text")
    assert texts == "Here is the answer."
    assert "<think" not in texts
