"""Backend dispatcher for `capmap agent`.

Two backends exist because an OpenAI-compatible endpoint (OpenAI itself,
Portkey, LiteLLM, Azure OpenAI, a self-hosted gateway, ...) is not Anthropic's
native Messages API — the two speak different wire formats for tool calls, so
there's a real implementation per backend rather than one client with a
`base_url` override. Picking a backend is a runtime choice, same pattern as
`onto/client.py`'s `resolve_backend` for the vocabulary (local YAML vs. an MCP
server): the caller doesn't need to know which one is running underneath.

Default, unconfigured behaviour is direct-to-Anthropic: no `OPENAI_BASE_URL`
set → `anthropic` backend.
"""
from __future__ import annotations

import os
from pathlib import Path

from epistemic_agent.project import Project


def _strip_env_value(raw: str) -> str:
    """Strip a trailing inline comment and surrounding quotes from one
    `.env.llm` value.

    A `#` only starts a comment when it's unquoted and preceded by
    whitespace (or is the entire value) — `OPENAI_MODEL=foo   # note` loses
    the note, but a quoted `"foo#bar"` keeps the `#` literally. Without this,
    a copy-pasted example line with a trailing explanatory comment silently
    becomes part of the value — e.g. a model ID with `# no default —
    deployment-specific` glued onto the end of it.
    """
    raw = raw.strip()
    if raw[:1] in "\"'":
        quote = raw[0]
        end = raw.find(quote, 1)
        return raw[1:end] if end != -1 else raw[1:]
    # Find the first '#' that actually starts a comment (at the very start,
    # or preceded by whitespace) — a '#' glued to the preceding character
    # (e.g. a URL fragment, `v1#section`) is left alone, and the search
    # keeps going past it in case a real comment follows later.
    idx = 0
    while True:
        hash_idx = raw.find("#", idx)
        if hash_idx == -1:
            return raw.strip()
        if hash_idx == 0 or raw[hash_idx - 1].isspace():
            return raw[:hash_idx].strip()
        idx = hash_idx + 1


def _load_env_file(start: Path) -> None:
    """Load KEY=VALUE pairs from the closest `.env.llm` found in `start` or
    any parent directory. Deliberately hand-rolled instead of adding
    `python-dotenv` as a dependency — the format needed is tiny (comments,
    blank lines, optional quotes). A variable already exported in the shell
    always wins (`setdefault`), so this is a fallback, not an override.
    """
    for d in [start, *start.parents]:
        env_file = d / ".env.llm"
        if not env_file.exists():
            continue
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = _strip_env_value(value)
            if key:
                os.environ.setdefault(key, value)
        return  # closest file wins — don't also load an ancestor's


def run_repl(project: Project, model: str | None = None, backend: str | None = None) -> None:
    _load_env_file(project.root)

    backend = backend or os.environ.get("CAPMAP_AGENT_BACKEND")
    if not backend:
        backend = "openai" if os.environ.get("OPENAI_BASE_URL") else "anthropic"

    if backend == "openai":
        from epistemic_agent.agent.backends.openai_backend import run_repl as _run
    elif backend == "anthropic":
        from epistemic_agent.agent.backends.anthropic_backend import run_repl as _run
    else:
        raise ValueError(f"Unknown agent backend {backend!r} — expected 'anthropic' or 'openai'")

    _run(project, model=model)
