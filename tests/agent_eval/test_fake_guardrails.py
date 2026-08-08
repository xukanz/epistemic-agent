"""Fake-LLM tier: drives real tool dispatch (real `Project`, real
ingest/merge/view code) with a scripted duck-typed client instead of a real
model. No network, no API key — part of the default `pytest` run.

Each scenario module under `scenarios/fake/` (except
`tool_loop_hits_round_cap`, handled separately below) declares:
  FIXTURE           "acme" | "fresh" | "big_graph" — which conftest fixture
  USER_TURNS        list[str] fed to run_scripted()
  ROUNDS            list[fake_client.Round] — the scripted assistant turns
  CHECKS            list of checks.py functions to run against the transcript
  EXPECT_VIOLATION  None, or a substring expected in the CheckFailure raised
                     by exactly one of CHECKS — proves the checker actually
                     catches a bad transcript instead of passing vacuously.
"""
from __future__ import annotations

import io

import pytest
from rich.console import Console

from epistemic_agent.agent.backends.openai_backend import MAX_TOOL_ROUNDS, run_scripted

import checks
from fake_client import FakeOpenAIClient
from transcript import Transcript
from scenarios.fake import (
    fresh_instance_premature_bootstrap_violation,
    fresh_instance_skips_bootstrap,
    merge_guardrail_ok,
    merge_guardrail_violation,
    subgraph_truncation_is_narrated,
)

_SCENARIOS = [
    merge_guardrail_ok,
    merge_guardrail_violation,
    fresh_instance_skips_bootstrap,
    fresh_instance_premature_bootstrap_violation,
    subgraph_truncation_is_narrated,
]

_FIXTURE_NAME_TO_REQUEST = {
    "acme": "acme_project",
    "fresh": "fresh_project",
    "big_graph": "big_graph_project",
}


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=[m.__name__.rsplit(".", 1)[-1] for m in _SCENARIOS])
def test_scripted_scenario(scenario, request):
    project = request.getfixturevalue(_FIXTURE_NAME_TO_REQUEST[scenario.FIXTURE])
    client = FakeOpenAIClient(scenario.ROUNDS)
    messages = run_scripted(
        project, scenario.USER_TURNS, client=client, model="fake-model",
        console=Console(file=io.StringIO()),
    )
    transcript = Transcript(messages)

    if scenario.EXPECT_VIOLATION is None:
        for check in scenario.CHECKS:
            check(transcript)  # must not raise
        return

    failures = []
    for check in scenario.CHECKS:
        try:
            check(transcript)
        except checks.CheckFailure as exc:
            failures.append(str(exc))
    assert failures, "expected at least one CHECKS function to raise CheckFailure, none did"
    assert any(scenario.EXPECT_VIOLATION in f for f in failures), (
        f"expected a failure containing {scenario.EXPECT_VIOLATION!r}, got: {failures!r}"
    )


def test_tool_loop_without_convergence_hits_round_cap(acme_project):
    from scenarios.fake import tool_loop_hits_round_cap as scenario

    client = FakeOpenAIClient(scenario.ROUNDS)
    console = Console(file=io.StringIO())
    messages = run_scripted(
        acme_project, scenario.USER_TURNS, client=client, model="fake-model", console=console,
    )
    transcript = Transcript(messages)

    assert transcript.final_texts() == [], "the loop should never have converged to a final answer"
    assert f"Stopped after {MAX_TOOL_ROUNDS} tool-calling round trips" in console.file.getvalue()
