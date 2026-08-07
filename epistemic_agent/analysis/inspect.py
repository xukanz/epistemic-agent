"""Drill into a single node.

The five analysis views answer organisation-level questions. This answers the
much more ordinary one you actually ask twenty times a day: *what is this node,
and what is it connected to?* Without it the only way to look at a specific
node is to grep the JSON, which does not resolve `k8s` → `tech-kubernetes` and
does not show you the edges.

Resolution order:

1. exact node ID, or exact `path` (a Repo's natural key) — unambiguous, return
2. otherwise take the **union** of the vocabulary hit and substring matches

Step 2 is a union rather than a fallback chain on purpose. A repo named
`atlas-agent` grounds to an internal system node `sys-atlas` if one exists with
that name, but someone typing that query almost certainly wants the
*repository*. Letting the vocabulary tier win silently answers a question the
user did not ask; showing both and asking costs one extra keystroke.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from epistemic_agent.analysis.views import GraphIndex, _label, _prop


def resolve_node(idx: GraphIndex, query: str, vocabulary=None) -> tuple[list[str], str]:
    """Return (matching node IDs, how they were found)."""
    if query in idx.nodes:
        return [query], "exact id"

    exact_path = [n["id"] for n in idx.nodes.values() if _prop(n, "path") == query]
    if exact_path:
        return exact_path, "exact path"

    vocab_hits: list[str] = []
    vocab_note = ""
    if vocabulary is not None:
        for target_type in ("TechStack", "Pattern"):
            res = vocabulary.ground(query, target_type=target_type)
            if res.best is not None and res.status == "grounded":
                hits = [
                    n["id"]
                    for n in idx.nodes.values()
                    if _prop(n, "term_id") == res.best.term_id
                ]
                if hits:
                    vocab_hits = hits
                    vocab_note = f"词表 → {res.best.term_id}（{res.best.label}）"
                    break

    q = query.lower()
    substring = [
        n["id"]
        for n in idx.nodes.values()
        if q in _label(n).lower() or q in _prop(n, "path").lower() or q in n["id"].lower()
    ]

    merged = list(dict.fromkeys(vocab_hits + sorted(substring)))
    if not merged:
        return [], "无匹配"
    if len(merged) == 1:
        return merged, vocab_note or "子串匹配"
    notes = [n for n in (vocab_note, f"子串匹配 {len(substring)} 个" if substring else "") if n]
    return merged, " + ".join(notes)


def vocabulary_hit_ids(idx: GraphIndex, query: str, vocabulary) -> set[str]:
    """Which of the candidates came from the vocabulary — used to mark them."""
    if vocabulary is None:
        return set()
    for target_type in ("TechStack", "Pattern"):
        res = vocabulary.ground(query, target_type=target_type)
        if res.best is not None and res.status == "grounded":
            return {
                n["id"] for n in idx.nodes.values() if _prop(n, "term_id") == res.best.term_id
            }
    return set()


def describe_node(idx: GraphIndex, node_id: str, neighbour_limit: int = 12) -> dict[str, Any]:
    """Everything worth knowing about one node, as plain data."""
    node = idx.nodes[node_id]
    props = dict(node.get("properties") or {})

    # System-maintained fields are separated so the interesting ones are not
    # buried under provenance strings.
    system = {k: v for k, v in props.items() if k.startswith("_")}
    business = {k: v for k, v in props.items() if not k.startswith("_")}

    out_edges: dict[str, list[str]] = defaultdict(list)
    in_edges: dict[str, list[str]] = defaultdict(list)
    for e in idx.edges:
        if e["source"] == node_id:
            out_edges[e["type"]].append(e["target"])
        if e["target"] == node_id:
            in_edges[e["type"]].append(e["source"])

    def summarise(edges: dict[str, list[str]], direction: str) -> list[dict]:
        rows = []
        for etype, others in sorted(edges.items()):
            by_type = Counter(idx.nodes[o]["type"] for o in others if o in idx.nodes)
            rows.append(
                {
                    "edge_type": etype,
                    "direction": direction,
                    "count": len(others),
                    "neighbour_types": dict(by_type),
                    "sample": [
                        {"id": o, "label": idx.label(o),
                         "type": idx.nodes.get(o, {}).get("type", "?")}
                        for o in sorted(others, key=idx.label)[:neighbour_limit]
                    ],
                    "truncated": max(0, len(others) - neighbour_limit),
                }
            )
        return rows

    # For a TechStack/Capability/Pattern node, "which teams" is the question
    # people actually have; the raw repo list is too long to read.
    team_breakdown: list[dict] = []
    if node["type"] in ("TechStack", "Capability", "Pattern", "InternalSystem"):
        counts: Counter = Counter()
        for etype in ("USES_TECH", "DEMONSTRATES", "IMPLEMENTS", "DEPENDS_ON_INTERNAL"):
            for repo in in_edges.get(etype, []):
                team = idx.repo_team.get(repo)
                counts[idx.label(team) if team else "(无归属)"] += 1
        team_breakdown = [{"team": t, "repos": c} for t, c in counts.most_common()]

    return {
        "id": node_id,
        "type": node["type"],
        "label": idx.label(node_id),
        "properties": business,
        "system_properties": system,
        "edges": summarise(out_edges, "out") + summarise(in_edges, "in"),
        "degree": sum(len(v) for v in out_edges.values())
        + sum(len(v) for v in in_edges.values()),
        "team_breakdown": team_breakdown,
    }


def render_node(console, info: dict, show_all_teams: bool = False) -> None:
    from rich.table import Table

    console.print(
        f"\n[bold]{info['label']}[/bold]  [dim]{info['id']}[/dim]\n"
        f"类型 [cyan]{info['type']}[/cyan] · 度数 {info['degree']}"
    )

    if info["properties"]:
        t = Table(box=None, show_header=False)
        t.add_column("", style="dim")
        t.add_column("", overflow="fold")
        for k, v in info["properties"].items():
            t.add_row(k, str(v))
        console.print(t)

    if info["edges"]:
        e = Table(title="关系", title_justify="left")
        for c in ("方向", "边类型", "数量", "邻居类型", "示例"):
            e.add_column(c, overflow="fold")
        for row in info["edges"]:
            arrow = "→ 出" if row["direction"] == "out" else "← 入"
            types = ", ".join(f"{k}×{v}" for k, v in row["neighbour_types"].items())
            sample = ", ".join(s["label"] for s in row["sample"])
            if row["truncated"]:
                sample += f" … 另 {row['truncated']} 个"
            e.add_row(arrow, row["edge_type"], str(row["count"]), types, sample)
        console.print(e)

    if info["team_breakdown"]:
        rows = info["team_breakdown"] if show_all_teams else info["team_breakdown"][:10]
        t = Table(title="按团队分布", title_justify="left")
        t.add_column("团队")
        t.add_column("仓库数", justify="right")
        for r in rows:
            t.add_row(r["team"], str(r["repos"]))
        console.print(t)
        rest = len(info["team_breakdown"]) - len(rows)
        if rest > 0:
            console.print(f"[dim]… 另 {rest} 个团队（--all-teams 看全部）[/dim]")

    sysp = info["system_properties"]
    if sysp:
        lines = []
        if sysp.get("_grounding_score") is not None:
            lines.append(f"接地置信度 {sysp['_grounding_score']}")
        if sysp.get("term_id"):
            lines.append(f"词表术语 {sysp['term_id']}")
        if sysp.get("_merged_from"):
            lines.append(f"合并自 {', '.join(sysp['_merged_from'])}")
        if sysp.get("_renamed_from"):
            lines.append(f"重命名自 {sysp['_renamed_from']}")
        if sysp.get("_last_updated"):
            lines.append(f"更新于 {sysp['_last_updated']}")
        if lines:
            console.print("[dim]" + " · ".join(lines) + "[/dim]")
        srcs = sysp.get("_sources", "")
        if srcs:
            n = len([s for s in str(srcs).split("; ") if s])
            console.print(f"[dim]来源 {n} 处（--json 看完整路径）[/dim]")


def build_index(kg: dict) -> GraphIndex:
    return GraphIndex(kg)
