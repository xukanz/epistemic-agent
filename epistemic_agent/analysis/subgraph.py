"""Bounded subgraph extraction.

The five analysis views (`views.py`) and node drill-down (`inspect.py`)
answer fixed-shape questions. Anything that instead needs "the neighbourhood
around these nodes, as data" — the retrieval half of a question-answering
feature built on top of the graph — has no primitive to reach for today:
`export --around/--hops` already walks a neighbourhood, but its only
consumer is a rendered file, not a JSON payload a script or an LLM can read.
This module is that primitive, kept deliberately dumb: no ranking, no LLM,
just BFS with a hard node cap so one high-degree node can't blow up the
result.
"""
from __future__ import annotations

from epistemic_agent.analysis.views import GraphIndex, _label


def _neighbours(idx: GraphIndex, node_id: str, edge_types: set[str] | None) -> set[str]:
    types = edge_types if edge_types is not None else set(idx.out) | set(idx.inb)
    found: set[str] = set()
    for et in types:
        found |= idx.out.get(et, {}).get(node_id, set())
        found |= idx.inb.get(et, {}).get(node_id, set())
    return found


def _node_view(idx: GraphIndex, node_id: str) -> dict:
    node = idx.nodes[node_id]
    return {
        "id": node_id,
        "type": node["type"],
        "label": _label(node),
        "properties": node.get("properties") or {},
    }


def _edge_view(edge: dict) -> dict:
    return {
        "id": edge.get("id"),
        "type": edge["type"],
        "source": edge["source"],
        "target": edge["target"],
    }


def bounded_subgraph(
    idx: GraphIndex,
    seed_ids: list[str],
    hops: int = 2,
    edge_types: set[str] | None = None,
    max_nodes: int = 60,
) -> dict:
    """BFS out from `seed_ids`, return the induced subgraph as plain data.

    Nodes are kept in BFS-distance order, so if `max_nodes` cuts the walk
    short, what survives is always the closest neighbourhood to the seeds —
    never an arbitrary slice. Seeds themselves are never dropped by the cap,
    even if there are more seeds than `max_nodes`.
    """
    seeds = [s for s in dict.fromkeys(seed_ids) if s in idx.nodes]
    if not seeds:
        return {"nodes": [], "edges": [], "seeds": [], "truncated": False}

    keep_ids: set[str] = set(seeds)
    order: list[str] = list(seeds)
    frontier = list(seeds)
    truncated = False

    for _ in range(max(0, hops)):
        if not frontier:
            break
        next_frontier: list[str] = []
        for node_id in frontier:
            for other in sorted(_neighbours(idx, node_id, edge_types)):
                if other in keep_ids:
                    continue
                if len(keep_ids) >= max_nodes:
                    truncated = True
                    continue
                keep_ids.add(other)
                order.append(other)
                next_frontier.append(other)
        frontier = next_frontier

    nodes = [_node_view(idx, nid) for nid in order]
    edges = [
        _edge_view(e)
        for e in idx.edges
        if e["source"] in keep_ids and e["target"] in keep_ids
    ]
    return {"nodes": nodes, "edges": edges, "seeds": seeds, "truncated": truncated}


def _edge_type_between(idx: GraphIndex, a: str, b: str) -> str | None:
    for e in idx.edges:
        if (e["source"] == a and e["target"] == b) or (e["source"] == b and e["target"] == a):
            return e["type"]
    return None


def _annotate_path(idx: GraphIndex, node_ids: list[str]) -> list[str]:
    out = [node_ids[0]]
    for a, b in zip(node_ids, node_ids[1:]):
        out.append(_edge_type_between(idx, a, b) or "?")
        out.append(b)
    return out


def path_between(
    idx: GraphIndex,
    source_id: str,
    target_id: str,
    max_hops: int = 4,
    max_paths: int = 5,
) -> dict:
    """Shortest path(s) between two nodes, direction ignored.

    Returns every shortest path found, up to `max_paths` — "how are these two
    connected" usually has more than one honest answer once ownership and
    dependency edges are in the mix, and picking one arbitrarily would hide
    that. Each path is `[node, edge_type, node, edge_type, node, ...]`.
    """
    if source_id not in idx.nodes or target_id not in idx.nodes:
        return {"paths": [], "found": False}
    if source_id == target_id:
        return {"paths": [[source_id]], "found": True}

    dist: dict[str, int] = {source_id: 0}
    parents: dict[str, list[str]] = {}
    frontier = [source_id]

    while frontier and target_id not in dist:
        current_dist = dist[frontier[0]]
        if current_dist >= max_hops:
            break
        next_dist = current_dist + 1
        next_frontier: list[str] = []
        seen_next: set[str] = set()
        for node_id in frontier:
            for other in _neighbours(idx, node_id, None):
                if other not in dist:
                    dist[other] = next_dist
                    parents.setdefault(other, []).append(node_id)
                    if other not in seen_next:
                        seen_next.add(other)
                        next_frontier.append(other)
                elif dist[other] == next_dist:
                    parents.setdefault(other, []).append(node_id)
        frontier = next_frontier

    if target_id not in dist:
        return {"paths": [], "found": False}

    paths: list[list[str]] = []

    def backtrack(node_id: str, tail: list[str]) -> None:
        if len(paths) >= max_paths:
            return
        if node_id == source_id:
            paths.append([source_id, *tail])
            return
        for p in parents.get(node_id, []):
            backtrack(p, [node_id, *tail])
            if len(paths) >= max_paths:
                return

    backtrack(target_id, [])
    return {"paths": [_annotate_path(idx, p) for p in paths], "found": True}
