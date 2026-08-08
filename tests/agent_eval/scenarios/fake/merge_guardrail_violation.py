"""Violating merge flow: `merge_apply` called with no preceding
`merge_dry_run` at all. Proves `checks.assert_merge_apply_after_dry_run`
actually fires on a bad transcript instead of passing vacuously.
"""
from fake_client import calls, text

import checks

FIXTURE = "acme"
USER_TURNS = ["Just merge the graph now."]
ROUNDS = [
    calls([("merge_apply", {})]),
    text("Done, merged."),
]
CHECKS = [checks.assert_merge_apply_after_dry_run]
EXPECT_VIOLATION = "merge_apply was called without a preceding merge_dry_run"
