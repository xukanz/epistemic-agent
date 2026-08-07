"""Backend dispatcher for `capmap agent`.

Two backends exist because a Portkey (or similar) gateway is an
OpenAI-compatible endpoint, not Anthropic's native Messages API — the two
speak different wire formats for tool calls, so there's a real implementation
per backend rather than one client with a `base_url` override. Picking a
backend is a runtime choice, same pattern as `onto/client.py`'s
`resolve_backend` for the vocabulary (local YAML vs. an MCP server): the
caller doesn't need to know which one is running underneath.

Default, unconfigured behaviour is direct-to-Anthropic: no `PORTKEY_BASE_URL`
set → `anthropic` backend.
"""
from __future__ import annotations

import os

from epistemic_agent.project import Project


def run_repl(project: Project, model: str | None = None, backend: str | None = None) -> None:
    backend = backend or os.environ.get("CAPMAP_AGENT_BACKEND")
    if not backend:
        backend = "portkey" if os.environ.get("PORTKEY_BASE_URL") else "anthropic"

    if backend == "portkey":
        from epistemic_agent.agent.backends.portkey_backend import run_repl as _run
    elif backend == "anthropic":
        from epistemic_agent.agent.backends.anthropic_backend import run_repl as _run
    else:
        raise ValueError(f"Unknown agent backend {backend!r} — expected 'anthropic' or 'portkey'")

    _run(project, model=model)
