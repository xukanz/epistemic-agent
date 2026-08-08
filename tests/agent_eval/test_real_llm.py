"""Real-LLM tier: drives `capmap agent` against a real OpenAI-compatible
endpoint (via `.env.llm`, same configuration `capmap agent` itself uses)
through scripted YAML scenarios in `scenarios/real/`.

Marked `llm_eval` and excluded from the default `pytest` run (see
`[tool.pytest.ini_options]` in pyproject.toml) — run explicitly with:

    pytest -m llm_eval -v

Every scenario is also checked, automatically, against
`checks.GLOBAL_INVARIANTS` (merge ordering, write-file allowlist, no crash
results, truncated-subgraph narration) regardless of what its own YAML
`assert:` blocks ask for — those encode the system prompt's non-negotiable
rules, not scenario-specific expectations.

Each turn is run one at a time (not the whole conversation up front) so a
turn's `assert:` block is checked against the transcript as it stood right
after that turn, not after later turns have added more tool calls.
"""
from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest
import yaml
from rich.console import Console

from epistemic_agent.agent.backends.openai_backend import _run_turn, build_openai_tools
from epistemic_agent.agent.prompt import build_system_prompt

import checks
from transcript import Transcript

pytestmark = pytest.mark.llm_eval

_SCENARIOS_DIR = Path(__file__).parent / "scenarios" / "real"
_ARTIFACTS_DIR = Path(__file__).resolve().parents[1] / "_artifacts" / "agent_eval"

_FIXTURE_NAME_TO_REQUEST = {
    "acme-corp": "acme_project",
    "fresh": "fresh_project",
}


def _load_scenario_paths() -> list[Path]:
    return sorted(_SCENARIOS_DIR.glob("*.yaml"))


@pytest.mark.parametrize("scenario_path", _load_scenario_paths(), ids=lambda p: p.stem)
def test_real_scenario(scenario_path, request, real_client):
    scenario = yaml.safe_load(scenario_path.read_text())
    client, model = real_client
    project = request.getfixturevalue(_FIXTURE_NAME_TO_REQUEST[scenario["fixture"]])

    specs, dispatch = build_openai_tools(project)
    tools = [
        {"type": "function", "function": {k: s[k] for k in ("name", "description", "parameters")}}
        for s in specs
    ]
    messages: list = [{"role": "system", "content": build_system_prompt(project)}]
    console = Console(file=io.StringIO())

    failures = []
    for i, turn in enumerate(scenario["turns"]):
        messages.append({"role": "user", "content": turn["user"]})
        _run_turn(client, model, messages, tools, dispatch, console)
        transcript = Transcript(messages)

        for spec in turn.get("assert") or []:
            try:
                checks.run_assertion(transcript, spec)
            except checks.CheckFailure as exc:
                failures.append(f"turn {i}: {exc}")
        try:
            checks.run_global_invariants(transcript)
        except checks.CheckFailure as exc:
            failures.append(f"turn {i} (global invariant): {exc}")

    artifact_path = _write_transcript(scenario["id"], messages)
    assert not failures, f"{len(failures)} assertion(s) failed (see {artifact_path}):\n" + "\n".join(failures)


def _write_transcript(scenario_id: str, messages: list) -> Path:
    out_dir = _ARTIFACTS_DIR / scenario_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{time.strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(json.dumps(messages, ensure_ascii=False, indent=2))
    return path
