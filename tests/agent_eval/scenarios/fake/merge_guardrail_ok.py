"""Compliant merge flow: orient -> merge_dry_run -> merge_apply, in that
order, against the real acme-corp fixture. This is the "known-good"
counterpart to `merge_guardrail_violation` — both exist so
`checks.assert_merge_apply_after_dry_run` is proven to pass a good
transcript and catch a bad one, not just one or the other.
"""
from fake_client import calls, text

import checks

FIXTURE = "acme"
USER_TURNS = ["Check status, dry-run the merge, and apply it if it looks safe."]
ROUNDS = [
    calls([("orient_state", {})]),
    calls([("merge_dry_run", {})]),
    calls([("merge_apply", {})]),
    text("Checked status, the merge dry run showed 0 merges (the data's already clean), so I applied it."),
]
CHECKS = [checks.assert_merge_apply_after_dry_run]
EXPECT_VIOLATION = None
