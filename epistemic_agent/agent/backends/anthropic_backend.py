"""Anthropic API backend — Claude API + Tool Runner, no Claude Code dependency.

`messages` is owned here, not by the Tool Runner: the runner does not expose
its internal history (see the Python SDK's own pause_turn-handling example,
which says as much), so a REPL that wants memory across turns has to keep its
own list and hand the full thing to a fresh `tool_runner()` call each turn.
"""
from __future__ import annotations

import os

import anthropic
from rich.console import Console

from epistemic_agent.agent.prompt import build_system_prompt
from epistemic_agent.agent.tools import build_tools
from epistemic_agent.project import Project

DEFAULT_MODEL = "claude-opus-5"


def _print_content(console: Console, content) -> None:
    for block in content:
        btype = getattr(block, "type", None)
        if btype == "text" and block.text.strip():
            console.print(block.text)
        elif btype == "tool_use":
            console.print(f"[dim]→ {block.name}({block.input})[/dim]")


def run_repl(project: Project, model: str | None = None) -> None:
    console = Console()
    client = anthropic.Anthropic()
    tools = build_tools(project)
    system = [
        {
            "type": "text",
            "text": build_system_prompt(project),
            "cache_control": {"type": "ephemeral"},
        }
    ]
    messages: list = []
    model = model or os.environ.get("CAPMAP_AGENT_MODEL", DEFAULT_MODEL)

    console.print(f"[bold]{project.name}[/bold] — capmap agent ({model}). Type 'exit' to quit.\n")
    while True:
        try:
            user_input = console.input("[bold cyan]> [/bold cyan]")
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not user_input.strip():
            continue
        if user_input.strip().lower() in {"exit", "quit"}:
            break

        messages.append({"role": "user", "content": user_input})
        runner = client.beta.messages.tool_runner(
            model=model,
            max_tokens=8096,
            system=system,
            tools=tools,
            messages=messages,
        )
        last = None
        try:
            for msg in runner:
                last = msg
                _print_content(console, msg.content)
        except TypeError as exc:
            # The SDK raises a bare TypeError, not an APIError, when it can't
            # resolve any credential at all (no env var, no `ant auth login`
            # profile) — it never gets far enough to make a request.
            if "authentication method" not in str(exc):
                raise
            messages.pop()
            console.print(
                "[red]No Anthropic credentials configured.[/red] Set ANTHROPIC_API_KEY, "
                "or run `ant auth login`, then try again."
            )
            continue
        except anthropic.APIError as exc:
            # Drop the just-appended user turn on failure — otherwise it sits
            # in history with no assistant reply, and the next successful
            # turn would silently fold it into whatever the user types next.
            messages.pop()
            console.print(f"[red]Request failed:[/red] {exc}")
            continue
        if last is not None:
            messages.append({"role": "assistant", "content": last.content})
