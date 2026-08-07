"""Portkey (or any OpenAI-compatible gateway) backend — function calling.

Portkey speaks OpenAI's `/chat/completions` wire format, not Anthropic's
native Messages API — `response.choices[0].message`, not `response.content`;
`tools`/`tool_calls`, not `tool_use`/`tool_result` blocks. That's why this is
a separate backend rather than pointing `anthropic.Anthropic` at a different
`base_url`: the two APIs are not compatible enough for that.

This backend is deliberately provider-agnostic — it knows nothing about which
model catalogue or ID format sits behind your gateway. Some deployments
front Bedrock (which needs a `us.`/`eu.` cross-region inference prefix on
Claude model IDs), others front Azure OpenAI or a direct Anthropic passthrough
with their own ID conventions — consult your own gateway's onboarding docs
for the endpoint and the exact model ID to use. Configuration is three env
vars, all required except the model override:

    PORTKEY_BASE_URL   your gateway's endpoint, e.g. https://<gateway>/v1
    PORTKEY_API_KEY    the API key Portkey issued for this workspace/config
    PORTKEY_MODEL       model ID exactly as your gateway expects it — there
                         is no default, because the right value is entirely
                         deployment-specific
"""
from __future__ import annotations

import json
import os

from portkey_ai import Portkey
from rich.console import Console

from epistemic_agent.agent.prompt import build_system_prompt
from epistemic_agent.agent.tools import build_openai_tools
from epistemic_agent.project import Project

# A tool-calling turn that hasn't stopped after this many round trips is
# almost certainly looping rather than converging — bail out instead of
# burning the gateway's rate limit.
MAX_TOOL_ROUNDS = 20


def run_repl(project: Project, model: str | None = None) -> None:
    console = Console()
    endpoint = os.environ.get("PORTKEY_BASE_URL")
    api_key = os.environ.get("PORTKEY_API_KEY")
    model = model or os.environ.get("PORTKEY_MODEL")
    missing = [
        name for name, val in [
            ("PORTKEY_BASE_URL", endpoint), ("PORTKEY_API_KEY", api_key), ("PORTKEY_MODEL", model),
        ] if not val
    ]
    if missing:
        console.print(
            f"[red]Missing configuration:[/red] {', '.join(missing)}. See your gateway's "
            "onboarding docs for the endpoint, API key, and the exact model ID it expects."
        )
        return

    client = Portkey(api_key=api_key, base_url=endpoint)
    specs, dispatch = build_openai_tools(project)
    tools = [
        {"type": "function", "function": {k: s[k] for k in ("name", "description", "parameters")}}
        for s in specs
    ]
    messages: list = [{"role": "system", "content": build_system_prompt(project)}]

    console.print(
        f"[bold]{project.name}[/bold] — capmap agent via Portkey ({model}). Type 'exit' to quit.\n"
    )
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
        # Roll back to here on failure — a turn can add several assistant/tool
        # messages across tool-calling round trips before something breaks.
        checkpoint = len(messages) - 1
        try:
            _run_turn(client, model, messages, tools, dispatch, console)
        except Exception as exc:  # noqa: BLE001 — any failure here should not crash the REPL
            del messages[checkpoint:]
            console.print(f"[red]Request failed:[/red] {exc}")


def _run_turn(client: Portkey, model: str, messages: list, tools: list, dispatch: dict,
              console: Console) -> None:
    for _ in range(MAX_TOOL_ROUNDS):
        resp = client.chat.completions.create(
            model=model, messages=messages, tools=tools, max_tokens=8096,
        )
        msg = resp.choices[0].message
        assistant_msg: dict = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.function.name, "arguments": call.function.arguments},
                }
                for call in msg.tool_calls
            ]
        messages.append(assistant_msg)

        if not msg.tool_calls:
            if msg.content:
                console.print(msg.content)
            return

        for call in msg.tool_calls:
            name = call.function.name
            args = json.loads(call.function.arguments or "{}")
            console.print(f"[dim]→ {name}({args})[/dim]")
            fn = dispatch.get(name)
            result = fn(**args) if fn is not None else f"Unknown tool: {name}"
            messages.append({"role": "tool", "tool_call_id": call.id, "content": str(result)})

    console.print(
        f"[yellow]Stopped after {MAX_TOOL_ROUNDS} tool-calling round trips without a final "
        "answer — this looks like a loop rather than progress.[/yellow]"
    )
