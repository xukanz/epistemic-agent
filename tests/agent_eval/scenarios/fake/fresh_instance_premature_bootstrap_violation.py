"""Violating first-run flow: the agent calls `run_bootstrap` immediately on a
fresh instance instead of holding the first-run-protocol conversation.
Proves the "no bootstrap before questions" check fires on a bad transcript,
mirroring `merge_guardrail_violation` for the merge guardrail.
"""
from fake_client import calls, text

import checks

FIXTURE = "fresh"
USER_TURNS = ["We want to track which teams use which tech."]
ROUNDS = [
    calls([("run_bootstrap", {})]),
    text("Bootstrapped the instance."),
]
CHECKS = [lambda t: checks.assert_tools_not_called(t, {"run_bootstrap", "ingest_payload"})]
EXPECT_VIOLATION = "forbidden tool(s) called"
