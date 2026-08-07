"""KG health manifest.

Two families of signal. The generic ones (ungrounded nodes, grounding
candidates, fuzzy duplicates, schema gaps, orphans, stale, deprecated) apply to
any graph built by this framework. `semantic_types` is config-driven rather
than a hard-coded constant, so a project with different node types gets a
correct check without editing this module. Fuzzy duplicate detection is
blocked by first character to keep it well short of O(n²) on a graph with
thousands of nodes.

The other four are specific to a capability map and are the reason this module
exists: bus factor, duplicated effort, uncovered vocabulary, and internal
lock-in.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

try:
    from rapidfuzz import fuzz

    _HAS_RAPIDFUZZ = True
except ImportError:  # pragma: no cover
    _HAS_RAPIDFUZZ = False

DEFAULT_SEMANTIC_TYPES = {"TechStack", "Capability", "Pattern", "InternalSystem"}
DEFAULT_FUZZY_THRESHOLD = 0.88
DEFAULT_STALE_DAYS = 365


def _prop(node: dict, key: str, default=""):
    return (node.get("properties") or {}).get(key, default) or default


def _label(node: dict) -> str:
    props = node.get("properties") or {}
    return props.get("label") or props.get("name") or node["id"]


class HealthManifest(BaseModel):
    generated_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    total_nodes: int = 0
    total_edges: int = 0
    node_counts: dict[str, int] = Field(default_factory=dict)
    edge_counts: dict[str, int] = Field(default_factory=dict)

    # --- generic quality signals (carried over) ---
    ungrounded_nodes: dict[str, list[str]] = Field(
        default_factory=dict, description="By node type: IDs with no term_id."
    )
    grounding_candidates: list[dict] = Field(
        default_factory=list, description="Nodes in the 0.50–0.70 band awaiting a decision."
    )
    fuzzy_duplicate_pairs: list[tuple[str, str, float]] = Field(
        default_factory=list, description="(id_a, id_b, score) above the similarity threshold."
    )
    schema_gap_clusters: list[dict] = Field(
        default_factory=list, description="Untyped nodes grouped by suggested_type."
    )
    orphan_nodes: list[str] = Field(default_factory=list)
    stale_nodes: list[str] = Field(
        default_factory=list, description="Repos with no human commit in > stale_days."
    )
    deprecated_nodes: list[str] = Field(default_factory=list)

    # --- capability-map signals (new) ---
    bus_factor_one: list[dict] = Field(
        default_factory=list,
        description="Capabilities/tech held by exactly one team — continuity risk.",
    )
    duplicate_clusters: list[dict] = Field(
        default_factory=list,
        description="Repos in one domain sharing most of their stack — consolidation candidates.",
    )
    uncovered_vocabulary: list[dict] = Field(
        default_factory=list,
        description="Vocabulary terms with no repo behind them — coverage gaps.",
    )
    internal_lockin: list[dict] = Field(
        default_factory=list,
        description="Internal systems ranked by how many repos depend on them.",
    )


# ---------------------------------------------------------------------------


def generate_manifest(
    kg_path: Path,
    output_path: Path | None = None,
    stale_days: int = DEFAULT_STALE_DAYS,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
    semantic_types: set[str] | None = None,
    vocabulary=None,
    min_cluster_stack: int = 3,
    min_cluster_jaccard: float = 0.5,
) -> HealthManifest:
    kg_path = Path(kg_path)
    if not kg_path.exists():
        raise FileNotFoundError(f"KG file not found: {kg_path}")

    kg = json.loads(kg_path.read_text())
    nodes: list[dict] = kg.get("nodes", [])
    edges: list[dict] = kg.get("edges", [])
    sem_types = semantic_types or DEFAULT_SEMANTIC_TYPES

    m = HealthManifest(total_nodes=len(nodes), total_edges=len(edges))

    node_counts: dict[str, int] = defaultdict(int)
    for n in nodes:
        node_counts[n["type"]] += 1
    m.node_counts = dict(sorted(node_counts.items()))
    edge_counts: dict[str, int] = defaultdict(int)
    for e in edges:
        edge_counts[e["type"]] += 1
    m.edge_counts = dict(sorted(edge_counts.items()))

    connected: set[str] = set()
    for e in edges:
        connected.add(e.get("source", ""))
        connected.add(e.get("target", ""))

    cutoff = (datetime.now() - timedelta(days=stale_days)).strftime("%Y-%m-%d")

    # --- per-node generic signals ---
    ungrounded: dict[str, list[str]] = defaultdict(list)
    for n in nodes:
        nid, ntype = n["id"], n["type"]
        props = n.get("properties") or {}

        if ntype in sem_types and not props.get("term_id"):
            ungrounded[ntype].append(nid)

        if props.get("_grounding_candidates"):
            m.grounding_candidates.append(
                {
                    "id": nid,
                    "type": ntype,
                    "label": _label(n),
                    "score": props.get("_grounding_score"),
                    "candidates": props["_grounding_candidates"],
                }
            )

        if nid not in connected:
            m.orphan_nodes.append(nid)

        # For a Repo the meaningful staleness signal is the last human commit,
        # not when the agent last touched the node.
        stamp = props.get("last_commit") or props.get("_last_updated") or ""
        if stamp and str(stamp)[:10] < cutoff:
            m.stale_nodes.append(nid)

        if props.get("status") == "deprecated":
            m.deprecated_nodes.append(nid)

    m.ungrounded_nodes = {k: sorted(v) for k, v in sorted(ungrounded.items())}

    # --- schema gaps ---
    untyped_by_suggestion: dict[str, list[dict]] = defaultdict(list)
    for n in nodes:
        if n["type"] == "Untyped":
            sug = _prop(n, "suggested_type") or "unknown"
            untyped_by_suggestion[sug].append({"id": n["id"], "label": _label(n)})
    for sug, members in sorted(untyped_by_suggestion.items(), key=lambda kv: -len(kv[1])):
        m.schema_gap_clusters.append(
            {"suggested_type": sug, "count": len(members), "members": members[:10]}
        )

    # --- fuzzy duplicates, blocked by first character ---
    if _HAS_RAPIDFUZZ:
        blocks: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for n in nodes:
            if n["type"] not in sem_types:
                continue
            lbl = _label(n).lower()
            if lbl:
                blocks[(n["type"], lbl[0])].append((lbl, n["id"]))
        for items in blocks.values():
            for i, (la, ida) in enumerate(items):
                for lb, idb in items[i + 1:]:
                    score = fuzz.token_sort_ratio(la, lb) / 100.0
                    if score >= fuzzy_threshold:
                        m.fuzzy_duplicate_pairs.append((ida, idb, round(score, 3)))
        m.fuzzy_duplicate_pairs.sort(key=lambda t: t[2], reverse=True)

    # --- capability-map signals ---
    _bus_factor(m, nodes, edges)
    _duplicate_clusters(m, nodes, edges, min_cluster_stack, min_cluster_jaccard)
    _internal_lockin(m, nodes, edges)
    if vocabulary is not None:
        _uncovered_vocabulary(m, nodes, vocabulary)

    if output_path is not None:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(m.model_dump_json(indent=2) + "\n")

    return m


# ---------------------------------------------------------------------------
# Capability-map signals


def _repo_team(edges: list[dict]) -> dict[str, str]:
    return {e["source"]: e["target"] for e in edges if e["type"] == "OWNED_BY"}


def _bus_factor(m: HealthManifest, nodes: list[dict], edges: list[dict]) -> None:
    """A capability held by one team is a continuity risk regardless of how
    many repos that team has — repos in one team share people."""
    repo_team = _repo_team(edges)
    labels = {n["id"]: _label(n) for n in nodes}
    holders: dict[str, set[str]] = defaultdict(set)
    repos_for: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        if e["type"] not in ("USES_TECH", "DEMONSTRATES", "IMPLEMENTS"):
            continue
        team = repo_team.get(e["source"])
        if team:
            holders[e["target"]].add(team)
        repos_for[e["target"]].add(e["source"])

    for target, teams in holders.items():
        if len(teams) == 1:
            m.bus_factor_one.append(
                {
                    "id": target,
                    "label": labels.get(target, target),
                    "sole_team": next(iter(teams)),
                    "repo_count": len(repos_for[target]),
                    "repos": sorted(repos_for[target])[:5],
                }
            )
    m.bus_factor_one.sort(key=lambda d: -d["repo_count"])


def _duplicate_clusters(
    m: HealthManifest,
    nodes: list[dict],
    edges: list[dict],
    min_stack: int,
    min_jaccard: float,
) -> None:
    """Repos in the same domain whose stacks overlap heavily.

    Emphatically a *candidate* signal: sharing LangGraph + FastAPI does not
    mean two teams built the same thing. The manifest reports it; the review
    queue asks a human; only then does a DUPLICATES edge get asserted.
    """
    repo_team = _repo_team(edges)
    labels = {n["id"]: _label(n) for n in nodes}
    stacks: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        if e["type"] == "USES_TECH":
            stacks[e["source"]].add(e["target"])
    domains: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        if e["type"] == "IN_DOMAIN":
            domains[e["target"]].append(e["source"])

    for domain, repos in sorted(domains.items()):
        repos = [r for r in repos if len(stacks.get(r, ())) >= min_stack]
        seen_pairs: list[dict] = []
        for i, a in enumerate(repos):
            for b in repos[i + 1:]:
                sa, sb = stacks[a], stacks[b]
                inter = sa & sb
                union = sa | sb
                if not union:
                    continue
                j = len(inter) / len(union)
                if j >= min_jaccard and len(inter) >= min_stack:
                    seen_pairs.append(
                        {
                            "domain": domain,
                            "repos": [a, b],
                            "repo_labels": [labels.get(a, a), labels.get(b, b)],
                            "teams": sorted({repo_team.get(a, "?"), repo_team.get(b, "?")}),
                            "shared_tech": sorted(inter),
                            "jaccard": round(j, 3),
                        }
                    )
        seen_pairs.sort(key=lambda d: (-d["jaccard"], -len(d["shared_tech"])))
        m.duplicate_clusters.extend(seen_pairs)
    m.duplicate_clusters.sort(key=lambda d: (-d["jaccard"], -len(d["shared_tech"])))


def _internal_lockin(m: HealthManifest, nodes: list[dict], edges: list[dict]) -> None:
    labels = {n["id"]: _label(n) for n in nodes}
    deps: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        if e["type"] == "DEPENDS_ON_INTERNAL":
            deps[e["target"]].add(e["source"])
    for sys_id, repos in deps.items():
        m.internal_lockin.append(
            {
                "id": sys_id,
                "label": labels.get(sys_id, sys_id),
                "dependent_repos": len(repos),
                "repos": sorted(repos)[:10],
            }
        )
    m.internal_lockin.sort(key=lambda d: -d["dependent_repos"])


def _uncovered_vocabulary(m: HealthManifest, nodes: list[dict], vocabulary) -> None:
    """Vocabulary terms nothing in the graph maps to.

    This is the only signal that can answer 'what does nobody here do?', and it
    is only as good as the vocabulary — which is the point of maintaining one.
    """
    covered = {
        (n.get("properties") or {}).get("term_id")
        for n in nodes
        if (n.get("properties") or {}).get("term_id")
    }
    for term in vocabulary._terms:  # noqa: SLF001 - same package, stable shape
        if term["term_id"] not in covered:
            m.uncovered_vocabulary.append(
                {
                    "term_id": term["term_id"],
                    "label": term["label"],
                    "shard": term["shard"],
                    "target_type": term["target_type"],
                }
            )
    m.uncovered_vocabulary.sort(key=lambda d: (d["shard"], d["term_id"]))


# ---------------------------------------------------------------------------


def print_summary(m: HealthManifest) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.print(f"\n[bold]KG Health Manifest[/bold] — {m.generated_at}")

    t = Table(show_header=False, box=None)
    t.add_row("Total nodes", str(m.total_nodes))
    t.add_row("Total edges", str(m.total_edges))
    console.print(t)

    if m.node_counts:
        nt = Table(title="Nodes by type", box=None, title_justify="left")
        nt.add_column("type")
        nt.add_column("count", justify="right")
        for k, v in m.node_counts.items():
            nt.add_row(k, str(v))
        console.print(nt)

    q = Table(title="Quality signals", box=None, title_justify="left")
    q.add_column("signal")
    q.add_column("count", justify="right")
    q.add_row("Ungrounded semantic nodes", str(sum(len(v) for v in m.ungrounded_nodes.values())))
    q.add_row("Grounding candidates (0.50–0.70)", str(len(m.grounding_candidates)))
    q.add_row("Fuzzy duplicate pairs", str(len(m.fuzzy_duplicate_pairs)))
    q.add_row("Schema gap clusters", str(len(m.schema_gap_clusters)))
    q.add_row("Orphan nodes", str(len(m.orphan_nodes)))
    q.add_row("Stale nodes", str(len(m.stale_nodes)))
    q.add_row("Deprecated nodes", str(len(m.deprecated_nodes)))
    console.print(q)

    c = Table(title="Capability-map signals", box=None, title_justify="left")
    c.add_column("signal")
    c.add_column("count", justify="right")
    c.add_row("Bus factor = 1 (single-team capability)", str(len(m.bus_factor_one)))
    c.add_row("Duplicate-effort candidates", str(len(m.duplicate_clusters)))
    c.add_row("Uncovered vocabulary terms", str(len(m.uncovered_vocabulary)))
    c.add_row("Internal systems with dependents", str(len(m.internal_lockin)))
    console.print(c)
