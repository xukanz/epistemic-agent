"""Unified LLM chat entry point with provider dispatch.

Usage:
    from epistemic_agent.llm.client import chat, chat_json

    text = chat(messages=[{"role": "user", "content": "Hello"}])

    data = chat_json(
        messages=[{"role": "system", "content": "Extract entities."},
                  {"role": "user", "content": abstract}],
        schema={"type": "object", "properties": {...}, "required": [...]},
    )

Model selection (in priority order):
    1. explicit `model=` kwarg
    2. env var `CAPMAP_LLM_MODEL`
    3. default `ollama/gemma4:latest`

Model format: `<provider>/<model_name>`
    provider ∈ {ollama, claude, openai}
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

DEFAULT_MODEL = os.environ.get("CAPMAP_LLM_MODEL", "ollama/gemma4:latest")
OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_KEY = "ollama"


def _split_model(model: str) -> tuple[str, str]:
    if "/" not in model:
        raise ValueError(f"Model must be '<provider>/<name>', got {model!r}")
    provider, _, name = model.partition("/")
    return provider, name


def _client(provider: str):
    cache = _client.__dict__.setdefault("_cache", {})
    if provider in cache:
        return cache[provider]
    if provider == "claude":
        import anthropic
        cache[provider] = anthropic.Anthropic()
    elif provider in ("ollama", "openai"):
        from openai import OpenAI
        if provider == "ollama":
            cache[provider] = OpenAI(base_url=OLLAMA_BASE, api_key=OLLAMA_KEY)
        else:
            cache[provider] = OpenAI()
    else:
        raise ValueError(f"Unknown provider {provider!r}")
    return cache[provider]


def chat(
    messages: list[dict],
    model: str | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.2,
    **kwargs: Any,
) -> str:
    """Return the assistant's text reply."""
    model = model or DEFAULT_MODEL
    provider, name = _split_model(model)
    client = _client(provider)

    if provider == "claude":
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        chat_msgs = [m for m in messages if m["role"] != "system"]
        create_kwargs: dict[str, Any] = dict(
            model=name,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=chat_msgs,
            **kwargs,
        )
        if system:
            create_kwargs["system"] = system
        resp = client.messages.create(**create_kwargs)
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

    resp = client.chat.completions.create(
        model=name,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        **kwargs,
    )
    return resp.choices[0].message.content or ""


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(text: str) -> str:
    m = _JSON_BLOCK.search(text)
    if m:
        text = m.group(1)
    depth = 0
    start = None
    best = None
    for i, c in enumerate(text):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start is not None:
                span = text[start : i + 1]
                if best is None or len(span) > len(best):
                    best = span
                start = None
    return best or text.strip()


def chat_json(
    messages: list[dict],
    schema: dict | None = None,
    model: str | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.1,
    retries: int = 2,
    **kwargs: Any,
) -> dict:
    """Return a parsed JSON object.

    On parse failure, retries up to `retries` times with a corrective prompt.
    """
    model = model or DEFAULT_MODEL
    provider, name = _split_model(model)

    if schema:
        schema_str = json.dumps(schema, indent=2)
        hint = (
            "Respond with a single JSON object matching this JSON Schema exactly. "
            "No prose, no markdown fences — JSON only.\n\n"
            f"Schema:\n{schema_str}"
        )
        has_system = any(m["role"] == "system" for m in messages)
        if has_system:
            messages = [
                {**m, "content": m["content"] + "\n\n" + hint} if m["role"] == "system" else m
                for m in messages
            ]
        else:
            messages = [{"role": "system", "content": hint}] + messages

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        call_kwargs = dict(kwargs)
        if provider in ("ollama", "openai"):
            call_kwargs.setdefault("response_format", {"type": "json_object"})
        text = chat(
            messages, model=model, max_tokens=max_tokens,
            temperature=temperature, **call_kwargs,
        )
        try:
            return json.loads(_extract_json(text))
        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
            if attempt < retries:
                messages = messages + [
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": (
                            f"The previous response was not valid JSON ({e}). "
                            "Emit the same answer as a single JSON object, no markdown."
                        ),
                    },
                ]
    raise RuntimeError(f"Failed to parse JSON after {retries + 1} attempts: {last_err}")
