"""OpenAI-compatible backend — function calling, works with any provider or
gateway that speaks the `/chat/completions` wire format.

This is not "the OpenAI backend" in the sense of only working with OpenAI's
own API — it's the backend for anything that speaks OpenAI's wire format:
Portkey, LiteLLM, Azure OpenAI, a self-hosted vLLM server, OpenAI itself, or
even Anthropic's own beta OpenAI-compatible endpoint
(`https://api.anthropic.com/v1` with an Anthropic API key and a Claude model
ID) — that last one means this single backend reaches Claude too, with no
separate Anthropic-native implementation needed. The trade-off: that
compatibility endpoint is a migration-oriented subset of Anthropic's real
API, so Claude-native features (extended thinking, some params) aren't
available through it, only through Anthropic's native Messages API — this
backend's wire format is `response.choices[0].message`, `tools`/`tool_calls`,
not `response.content`, `tool_use`/`tool_result` blocks.

Implemented with the plain `openai` SDK, not a provider-specific package —
`openai.OpenAI(base_url=..., api_key=..., default_headers=...)` works against
any endpoint of this shape. Some gateways (Portkey included) additionally
require the key on a specific header rather than (or in addition to) the
standard `Authorization: Bearer` the SDK sends automatically — that's what
`OPENAI_EXTRA_HEADER` is for below.

This backend is deliberately provider-agnostic beyond that — it knows nothing
about which model catalogue or ID format sits behind your endpoint. Some
deployments front Bedrock (which needs a `us.`/`eu.` cross-region inference
prefix on Claude model IDs), others front Azure OpenAI, OpenAI directly, or a
gateway with its own ID conventions — consult your provider's docs for the
endpoint and the exact model ID to use. Configuration:

    OPENAI_BASE_URL     the endpoint, e.g. https://api.openai.com/v1 or your
                         gateway's URL
    OPENAI_API_KEY      your API key, sent as `Authorization: Bearer ...`
    OPENAI_MODEL        model ID exactly as the endpoint expects it — there
                         is no default, because the right value is entirely
                         deployment-specific
    OPENAI_EXTRA_HEADER optional — a header name to also carry the API key
                         on, for gateways that need it there instead of (or
                         in addition to) Bearer auth. Portkey needs this set
                         to `x-portkey-api-key`; most OpenAI-compatible
                         endpoints don't need it at all.
"""
from __future__ import annotations

import io
import json
import os

from openai import OpenAI
from rich.console import Console

from epistemic_agent.agent.prompt import build_system_prompt
from epistemic_agent.agent.tools import build_openai_tools
from epistemic_agent.project import Project

# A tool-calling turn that hasn't stopped after this many round trips is
# almost certainly looping rather than converging — bail out instead of
# burning the gateway's rate limit.
MAX_TOOL_ROUNDS = 20


def build_client_from_env(model: str | None = None) -> tuple[OpenAI, str]:
    """Read `OPENAI_BASE_URL`/`OPENAI_API_KEY`/`OPENAI_MODEL`/`OPENAI_EXTRA_HEADER`
    from the environment and construct the client `run_repl` needs.

    Raises `RuntimeError` (message matches `run_repl`'s prior inline check) if
    any required value is missing, so callers driving the agent
    programmatically (the eval harness) can turn that into a clean skip
    instead of a crash.
    """
    endpoint = os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("OPENAI_API_KEY")
    model = model or os.environ.get("OPENAI_MODEL")
    extra_header = os.environ.get("OPENAI_EXTRA_HEADER")
    missing = [
        name for name, val in [
            ("OPENAI_BASE_URL", endpoint), ("OPENAI_API_KEY", api_key), ("OPENAI_MODEL", model),
        ] if not val
    ]
    if missing:
        raise RuntimeError(
            f"Missing configuration: {', '.join(missing)}. See your provider's docs for the "
            "endpoint, API key, and the exact model ID it expects."
        )
    default_headers = {extra_header: api_key} if extra_header else None
    client = OpenAI(api_key=api_key, base_url=endpoint, default_headers=default_headers)
    return client, model


def run_scripted(
    project: Project, user_turns: list[str], *, client: OpenAI, model: str,
    console: Console | None = None,
) -> list[dict]:
    """Drive one full conversation from a fixed list of user turns, with no
    interactive input, and return the accumulated `messages` transcript.

    `client` is duck-typed the same way `_run_turn` treats it — a real
    `OpenAI` client or a scripted fake both work — which is what lets the
    eval harness reuse this for both the no-network fake-LLM tier and the
    real-endpoint tier instead of re-deriving the system prompt/tools/dispatch
    wiring `run_repl` already knows how to do.
    """
    console = console or Console(file=io.StringIO())
    specs, dispatch = build_openai_tools(project)
    tools = [
        {"type": "function", "function": {k: s[k] for k in ("name", "description", "parameters")}}
        for s in specs
    ]
    messages: list = [{"role": "system", "content": build_system_prompt(project)}]
    for user_text in user_turns:
        messages.append({"role": "user", "content": user_text})
        _run_turn(client, model, messages, tools, dispatch, console)
    return messages


def run_repl(project: Project, model: str | None = None) -> None:
    console = Console()
    try:
        client, model = build_client_from_env(model)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    specs, dispatch = build_openai_tools(project)
    tools = [
        {"type": "function", "function": {k: s[k] for k in ("name", "description", "parameters")}}
        for s in specs
    ]
    messages: list = [{"role": "system", "content": build_system_prompt(project)}]

    console.print(
        f"[bold]{project.name}[/bold] — capmap agent via OpenAI-compatible endpoint ({model}). "
        "Type 'exit' to quit.\n"
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


def _run_turn(client: OpenAI, model: str, messages: list, tools: list, dispatch: dict,
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
