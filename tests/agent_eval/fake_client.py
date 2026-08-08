"""A duck-typed stand-in for `openai.OpenAI`, scripted instead of driven by a
real model.

`_run_turn` in `epistemic_agent/agent/backends/openai_backend.py` only ever
calls `client.chat.completions.create(model=..., messages=..., tools=...,
max_tokens=...)` and reads `resp.choices[0].message.{content, tool_calls}`,
where each `tool_calls[i]` has `.id`, `.function.name`, `.function.arguments`
(a JSON string). That's the entire surface to fake — everything is
duck-typed, so no `openai` SDK types are needed.
"""
from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field


@dataclass
class _FunctionCall:
    name: str
    arguments: str


@dataclass
class _ToolCall:
    id: str
    function: _FunctionCall


@dataclass
class _Message:
    content: str | None
    tool_calls: list[_ToolCall] = field(default_factory=list)


@dataclass
class _Choice:
    message: _Message


@dataclass
class _Response:
    choices: list[_Choice]


class Round:
    """One scripted assistant turn: either one or more tool calls, or a final
    natural-language answer — never both, matching what a real model turn
    looks like on the wire."""

    def __init__(self, tool_calls: list[tuple[str, dict]] | None = None, content: str | None = None):
        self.tool_calls = tool_calls or []
        self.content = content


def calls(tool_calls: list[tuple[str, dict]]) -> Round:
    """An assistant turn that requests tool calls, with no final text yet.
    `tool_calls` is `[(name, args_dict), ...]`."""
    return Round(tool_calls=tool_calls)


def text(content: str) -> Round:
    """An assistant turn that ends the exchange with a final answer and no
    tool calls."""
    return Round(content=content)


class _Completions:
    def __init__(self, client: "FakeOpenAIClient"):
        self._client = client

    def create(self, *, model, messages, tools=None, max_tokens=None):
        return self._client._create(model=model, messages=messages, tools=tools, max_tokens=max_tokens)


class _Chat:
    def __init__(self, client: "FakeOpenAIClient"):
        self.completions = _Completions(client)


class FakeOpenAIClient:
    """Feeds one scripted `Round` per `.create()` call, in order, regardless
    of what `messages`/`tools` it's actually passed — the scenario author is
    responsible for scripting a plausible sequence given what the real tool
    dispatch will return.

    Running out of scripted rounds raises a clear `AssertionError` (did the
    scenario forget a trailing `text(...)` round?) instead of a confusing
    `StopIteration` bubbling out of `_run_turn`.
    """

    def __init__(self, rounds: list[Round]):
        self._rounds = iter(rounds)
        self._id_counter = itertools.count()
        self.calls_seen: list[dict] = []
        self.chat = _Chat(self)

    def _create(self, *, model, messages, tools=None, max_tokens=None):
        self.calls_seen.append({"model": model, "messages": list(messages), "tools": tools})
        try:
            round_ = next(self._rounds)
        except StopIteration:
            raise AssertionError(
                "FakeOpenAIClient script exhausted — the scripted conversation asked for "
                "another model turn than there were Round()s for. Did you forget a "
                "trailing text(...) round?"
            ) from None

        tool_calls = [
            _ToolCall(id=f"call_{next(self._id_counter)}", function=_FunctionCall(name=name, arguments=json.dumps(args)))
            for name, args in round_.tool_calls
        ]
        return _Response(choices=[_Choice(message=_Message(content=round_.content, tool_calls=tool_calls))])
