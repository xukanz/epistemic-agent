"""Compliant long-tail retrieval: the agent calls `subgraph` against a graph
big enough that the tool's own truncation kicks in (see `85c9dfa`'s
truncation fix), and correctly caveats the answer as partial rather than
presenting it as exhaustive.
"""
from fake_client import calls, text

import checks

FIXTURE = "big_graph"
USER_TURNS = ["What's connected to the hub repo?"]
ROUNDS = [
    calls([("subgraph", {"seeds": ["hub"], "hops": 1, "max_nodes": 80})]),
    text(
        "The hub repo connects to many others, but I could only show a partial "
        "neighbourhood here — the result is truncated. Ask me to narrow the query "
        "(fewer hops or a smaller max_nodes) to see more of it."
    ),
]
CHECKS = [checks.assert_truncated_subgraph_is_narrated]
EXPECT_VIOLATION = None
