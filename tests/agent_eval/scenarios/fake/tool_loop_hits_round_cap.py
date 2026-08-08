"""A script that never emits a final answer — every round requests another
`orient_state` call — to verify `MAX_TOOL_ROUNDS` in `_run_turn` is honoured:
the loop bails after 20 round trips and reports rather than hanging or
crashing. Checked by its own dedicated test (console output + no final
answer), not the generic CHECKS/EXPECT_VIOLATION pattern the other fake
scenarios use.
"""
from fake_client import calls

FIXTURE = "acme"
USER_TURNS = ["Keep checking status until I say stop."]
ROUNDS = [calls([("orient_state", {})]) for _ in range(25)]
