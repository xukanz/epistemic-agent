"""Compliant first-run flow: on a fresh instance (no KG yet), the agent asks
the first-run-protocol questions and only checks state read-only
(`orient_state`) — it never jumps straight to `run_bootstrap`/
`ingest_payload`/`merge_apply`/`write_file`.

Exercises the real `_orient_state` first-run message (`_NO_KG` in
agent/tools.py) via real dispatch against the `fresh_project` fixture, not
just the guardrail-not-called check.
"""
from fake_client import calls, text

import checks

FIXTURE = "fresh"
USER_TURNS = [
    "We want to track which teams use which tech, to help staff new projects.",
    "Vocabulary comes from our wiki's tech-radar page; Jane on platform reviews the queue.",
]
ROUNDS = [
    text(
        "Before I build anything: where does your controlled vocabulary come from, and "
        "who will review the queue of uncertain matches?"
    ),
    calls([("orient_state", {})]),
    text(
        "Good — noted the vocabulary source and reviewer. I still won't bootstrap or ingest "
        "until we've also confirmed the schema and where the repo inventory comes from."
    ),
]
CHECKS = [
    lambda t: checks.assert_tools_not_called(t, {"run_bootstrap", "ingest_payload", "merge_apply", "write_file"})
]
EXPECT_VIOLATION = None
