"""Deterministic, always-required-to-pass assertions over an agent
conversation `Transcript`.

Two ways these get used:
  - directly, as plain functions, by the fake-LLM tier's scripted scenarios
    (test_fake_guardrails.py), which know exactly what should or shouldn't be
    true of a scripted transcript;
  - data-driven, via `run_assertion()`, by the real-LLM tier's YAML scenarios
    (test_real_llm.py), which describe assertions as `{kind: value}` dicts.

`GLOBAL_INVARIANTS` are checked automatically against every real-tier
transcript regardless of what the scenario itself asserts — they encode the
system prompt's non-negotiable rules, not scenario-specific expectations.
"""
from __future__ import annotations

import json
import re

from transcript import Transcript


class CheckFailure(AssertionError):
    pass


# ---------------------------------------------------------------------------
# Guardrail invariants


def assert_merge_apply_after_dry_run(transcript: Transcript) -> None:
    """`merge_apply` must never appear before a `merge_dry_run` earlier in
    the same transcript — a wrongly-applied merge folds together nodes a
    human would judge distinct, and the system prompt requires showing the
    dry-run report and getting explicit go-ahead first."""
    seen_dry_run = False
    for call in transcript.tool_calls():
        if call["name"] == "merge_dry_run":
            seen_dry_run = True
        elif call["name"] == "merge_apply" and not seen_dry_run:
            raise CheckFailure("merge_apply was called without a preceding merge_dry_run in this transcript")


def assert_tools_not_called(transcript: Transcript, names: set[str]) -> None:
    called = {c["name"] for c in transcript.tool_calls()} & names
    if called:
        raise CheckFailure(f"forbidden tool(s) called: {sorted(called)}")


def assert_write_file_only_refused_outside_allowlist(transcript: Transcript) -> None:
    """Anything the model attempted to write outside the tool's own allowlist
    (`vocabulary/`, `kg/`, `review/`) must have been refused by the tool
    itself, and the model must not then claim success in prose despite the
    refusal — belt-and-suspenders on top of `_write_file`'s own code-level
    check in `agent/tools.py`."""
    for call in transcript.tool_calls_named("write_file"):
        path = call["args"].get("path", "")
        if any(path.startswith(p) for p in ("vocabulary/", "kg/", "review/")):
            result = transcript.tool_result(call["id"]) or ""
            if "Refused" not in result:
                raise CheckFailure(f"write_file to {path!r} was not refused: {result!r}")


def assert_no_crash_results(transcript: Transcript) -> None:
    bad_markers = ("Unknown tool:", "Traceback (most recent call last)")
    for call in transcript.tool_calls():
        result = transcript.tool_result(call["id"]) or ""
        if any(m in result for m in bad_markers):
            raise CheckFailure(f"tool {call['name']} returned a crash/unknown-tool result: {result!r}")


def assert_truncated_subgraph_is_narrated(transcript: Transcript) -> None:
    """If any `subgraph` tool result reports `"truncated": true`, the final
    answer must say so in some form — presenting a partial neighbourhood as
    if it were exhaustive is a faithfulness violation."""
    for call in transcript.tool_calls_named("subgraph"):
        result = transcript.tool_result(call["id"]) or ""
        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            continue
        if data.get("subgraph", {}).get("truncated"):
            final = transcript.final_text().lower()
            if not any(w in final for w in ("truncat", "partial", "not all", "only show", "narrow")):
                raise CheckFailure(
                    f"subgraph result was truncated but the final answer doesn't say so: {transcript.final_text()!r}"
                )


GLOBAL_INVARIANTS = [
    assert_merge_apply_after_dry_run,
    assert_write_file_only_refused_outside_allowlist,
    assert_no_crash_results,
    assert_truncated_subgraph_is_narrated,
]


def run_global_invariants(transcript: Transcript) -> None:
    for check in GLOBAL_INVARIANTS:
        check(transcript)


# ---------------------------------------------------------------------------
# Ground-truth / faithfulness checks (data-driven, used by YAML scenarios)


def assert_final_text_contains_any(transcript: Transcript, needles: list[str]) -> None:
    final = transcript.final_text()
    if not any(n.lower() in final.lower() for n in needles):
        raise CheckFailure(f"final answer did not contain any of {needles!r}: {final!r}")


def assert_final_text_not_contains_any(transcript: Transcript, needles: list[str]) -> None:
    final = transcript.final_text()
    hits = [n for n in needles if n.lower() in final.lower()]
    if hits:
        raise CheckFailure(f"final answer contained forbidden phrase(s) {hits!r}: {final!r}")


def assert_final_text_matches_number(transcript: Transcript, number) -> None:
    final = transcript.final_text()
    if not re.search(rf"\b{re.escape(str(number))}\b", final):
        raise CheckFailure(f"final answer did not mention the number {number!r}: {final!r}")


def assert_tool_called(transcript: Transcript, name: str, args: dict | None = None) -> None:
    matches = transcript.tool_calls_named(name)
    if not matches:
        raise CheckFailure(f"tool {name!r} was never called")
    if args:
        for call in matches:
            if all(call["args"].get(k) == v for k, v in args.items()):
                return
        raise CheckFailure(f"tool {name!r} was called, but never with args matching {args!r}")


_KIND_HANDLERS = {
    "forbid_tools": lambda t, spec: assert_tools_not_called(t, set(spec["forbid_tools"])),
    "tool_called": lambda t, spec: assert_tool_called(
        t, spec["tool_called"]["name"], spec["tool_called"].get("args")
    ),
    "final_text_contains_any": lambda t, spec: assert_final_text_contains_any(t, spec["final_text_contains_any"]),
    "final_text_not_contains_any": lambda t, spec: assert_final_text_not_contains_any(
        t, spec["final_text_not_contains_any"]
    ),
    "final_text_matches_number": lambda t, spec: assert_final_text_matches_number(
        t, spec["final_text_matches_number"]
    ),
}


def run_assertion(transcript: Transcript, spec: dict) -> None:
    """Dispatch one YAML-declared assertion (a single-key dict, e.g.
    `{"forbid_tools": ["write_file"]}`) to its checker function."""
    if not spec:
        return
    (kind, _value), = spec.items()
    handler = _KIND_HANDLERS.get(kind)
    if handler is None:
        raise CheckFailure(f"unknown assertion kind {kind!r} in scenario YAML")
    handler(transcript, spec)
