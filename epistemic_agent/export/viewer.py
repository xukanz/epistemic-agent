"""Build a self-contained HTML viewer.

Three decisions worth explaining:

**Self-contained, no CDN.** The output is one file you can double-click, mail
to someone, or open on a machine with no network. A capability map names teams
and rates their work; needing to serve it from somewhere adds a deployment
question to what should be "here, look at this".

**Views, not one hairball.** 929 nodes and 2118 edges drawn at once is a grey
blob that answers nothing. The interesting structures are subgraphs, so the
viewer ships several and defaults to the readable one:

* `tech` — technology co-occurrence: two technologies linked when ≥N repos use
  both. Answers "what do we build things out of, and what goes with what".
* `team` — team ↔ technology bipartite. Answers "who does what".
* `domain` — repo ↔ domain ↔ technology. Answers "which parts of the business
  use which stack".
* `full` — everything. Included because people ask for it; labelled as hard to
  read because it is.

**Layout runs in the browser, not here.** A force simulation is iterative and
people want to watch it settle and re-run it. Precomputing coordinates in
Python would need a numeric stack this project does not otherwise depend on.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

TEMPLATE = Path(__file__).parent / "template.html"

TYPE_COLORS = {
    "Repo": "#4C8BF5",
    "Team": "#34A853",
    "TechStack": "#FBBC05",
    "Capability": "#EA4335",
    "Pattern": "#A142F4",
    "Domain": "#00ACC1",
    "InternalSystem": "#F4511E",
    "Person": "#8D6E63",
    "Untyped": "#9E9E9E",
}

# Properties worth showing in the detail panel, in display order. Everything
# else is hidden — `_sources` alone would fill the panel.
DETAIL_PROPS = [
    "label", "name", "path", "url", "description", "value", "replace",
    "tier", "reuse", "kind", "namespace", "last_commit", "star_count",
    "term_id", "ontology_id", "_grounding_score", "raw_label", "_merged_from",
]


def _label(node: dict) -> str:
    p = node.get("properties") or {}
    return p.get("label") or p.get("name") or node["id"]


def _detail(node: dict) -> dict:
    p = node.get("properties") or {}
    out = {}
    for k in DETAIL_PROPS:
        v = p.get(k)
        if v in (None, "", [], {}):
            continue
        out[k] = "; ".join(str(x) for x in v) if isinstance(v, list) else v
    return out


def _pack(nodes: list[dict], edges: list[tuple[str, str, str]]) -> dict:
    """Index-based packing. Node IDs repeat in every edge; at 2118 edges the
    string form roughly triples the payload for no benefit."""
    ids = [n["id"] for n in nodes]
    pos = {nid: i for i, nid in enumerate(ids)}
    types = sorted({n["type"] for n in nodes})
    tpos = {t: i for i, t in enumerate(types)}
    etypes = sorted({e[2] for e in edges})
    epos = {t: i for i, t in enumerate(etypes)}

    deg: Counter = Counter()
    for s, t, _ in edges:
        deg[s] += 1
        deg[t] += 1

    return {
        "types": types,
        "edgeTypes": etypes,
        "nodes": [
            {
                "i": pos[n["id"]],
                "id": n["id"],
                "l": _label(n),
                "t": tpos[n["type"]],
                "d": deg[n["id"]],
                "p": _detail(n),
            }
            for n in nodes
        ],
        "edges": [
            {"s": pos[s], "t": pos[t], "y": epos[ty]}
            for s, t, ty in edges
            if s in pos and t in pos
        ],
    }


# ---------------------------------------------------------------------------
# Views


def _view_full(kg: dict) -> dict:
    edges = [(e["source"], e["target"], e["type"]) for e in kg["edges"]]
    return _pack(kg["nodes"], edges)


def _view_tech_cooccurrence(kg: dict, min_shared: int = 3) -> dict:
    """Technologies linked when ≥min_shared repos use both."""
    by_id = {n["id"]: n for n in kg["nodes"]}
    repo_tech: dict[str, list[str]] = defaultdict(list)
    for e in kg["edges"]:
        if e["type"] == "USES_TECH":
            repo_tech[e["source"]].append(e["target"])

    pair: Counter = Counter()
    for techs in repo_tech.values():
        uniq = sorted(set(techs))
        for i, a in enumerate(uniq):
            for b in uniq[i + 1:]:
                pair[(a, b)] += 1

    edges = [(a, b, "CO_OCCURS") for (a, b), c in pair.items() if c >= min_shared]
    keep = {x for a, b, _ in edges for x in (a, b)}
    nodes = [by_id[i] for i in sorted(keep) if i in by_id]
    return _pack(nodes, edges)


def _view_team_tech(kg: dict, min_repos: int = 1) -> dict:
    """Team ↔ technology, weighted by how many of the team's repos use it."""
    by_id = {n["id"]: n for n in kg["nodes"]}
    repo_team = {e["source"]: e["target"] for e in kg["edges"] if e["type"] == "OWNED_BY"}
    pair: Counter = Counter()
    for e in kg["edges"]:
        if e["type"] == "USES_TECH":
            team = repo_team.get(e["source"])
            if team:
                pair[(team, e["target"])] += 1

    edges = [(t, k, "TEAM_USES") for (t, k), c in pair.items() if c >= min_repos]
    keep = {x for a, b, _ in edges for x in (a, b)}
    nodes = [by_id[i] for i in sorted(keep) if i in by_id]
    return _pack(nodes, edges)


def _view_domain(kg: dict) -> dict:
    """Domain ↔ technology, via the repos in each domain."""
    by_id = {n["id"]: n for n in kg["nodes"]}
    repo_domain = {e["source"]: e["target"] for e in kg["edges"] if e["type"] == "IN_DOMAIN"}
    pair: Counter = Counter()
    for e in kg["edges"]:
        if e["type"] == "USES_TECH":
            dom = repo_domain.get(e["source"])
            if dom:
                pair[(dom, e["target"])] += 1

    edges = [(d, t, "DOMAIN_USES") for (d, t), c in pair.items() if c >= 2]
    keep = {x for a, b, _ in edges for x in (a, b)}
    nodes = [by_id[i] for i in sorted(keep) if i in by_id]
    return _pack(nodes, edges)


VIEW_BUILDERS = {
    "tech": ("技术共现", "两个技术被 ≥3 个仓库同时使用就连一条边", _view_tech_cooccurrence),
    "team": ("团队 ↔ 技术", "谁在用什么", _view_team_tech),
    "domain": ("领域 ↔ 技术", "哪块业务用哪些栈（≥2 个仓库才连边）", _view_domain),
    "full": ("全图", "所有节点和边。能看结构密度，看不清细节", _view_full),
}


# ---------------------------------------------------------------------------


def build_html(kg: dict, title: str = "capability map", default_view: str = "tech") -> str:
    if not TEMPLATE.exists():
        raise FileNotFoundError(f"viewer template missing: {TEMPLATE}")

    views = {}
    for key, (name, desc, fn) in VIEW_BUILDERS.items():
        packed = fn(kg)
        packed["name"] = name
        packed["desc"] = desc
        views[key] = packed

    payload = {
        "title": title,
        "defaultView": default_view if default_view in views else "tech",
        "colors": TYPE_COLORS,
        "views": views,
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # `</script>` inside embedded JSON would close the tag early.
    data = data.replace("</", "<\\/")

    html = TEMPLATE.read_text()
    return html.replace("__CAPMAP_TITLE__", title).replace('"__CAPMAP_DATA__"', data)


def view_summary(kg: dict) -> list[dict]:
    """What each view will contain — printed by the CLI so nobody opens a
    500-node picture expecting to read the labels."""
    rows = []
    for key, (name, desc, fn) in VIEW_BUILDERS.items():
        packed = fn(kg)
        rows.append(
            {
                "key": key,
                "name": name,
                "nodes": len(packed["nodes"]),
                "edges": len(packed["edges"]),
                "desc": desc,
            }
        )
    return rows


_EXPORT_EXTENSIONS = {"html": "html", "graphml": "graphml", "gexf": "gexf",
                      "cypher": "cypher", "dot": "dot"}

_EXPORT_TIPS = {
    "html": "Open directly in a browser (double-click, or xdg-open/open) — no network needed.",
    "graphml": "Import into Gephi / Cytoscape / yEd, or read with networkx.read_graphml().",
    "gexf": "Import into Gephi — its native format, friendlier property panel.",
    "cypher": "cat <file> | cypher-shell -u neo4j -p <password>. Sample queries at the end of the file.",
    "dot": "dot -Tsvg <file> -o out.svg (or neato/fdp for mesh-like graphs).",
}


def run_export(
    proj,
    fmt: str = "html",
    output=None,
    default_view: str = "tech",
    node_type: str | None = None,
    around: str | None = None,
    hops: int = 1,
    keep_sources: bool = False,
) -> dict:
    """Filter and render the KG to a file. Shared by `cli.py`'s `export`
    command and the agent's `export` tool, so the neighbourhood/type
    filtering logic (see the module docstring's caveats about disconnected
    subgraphs) is implemented once. Returns a plain dict rather than
    printing, so callers decide how to present it.
    """
    from pathlib import Path

    from epistemic_agent.export.formats import WRITERS

    kg = proj.load_kg()
    notes: list[str] = []

    if around:
        from epistemic_agent.analysis.inspect import build_index, resolve_node

        idx = build_index(kg)
        seeds, how = resolve_node(idx, around, proj.vocabulary())
        if not seeds:
            return {"error": f"Could not resolve {around!r}."}
        notes.append(f"{len(seeds)} seed node(s) ({how}), expanded {hops} hop(s)")
        frontier, keep_ids = set(seeds), set(seeds)
        for _ in range(max(0, hops)):
            nxt: set[str] = set()
            for e in kg["edges"]:
                if e["source"] in frontier:
                    nxt.add(e["target"])
                if e["target"] in frontier:
                    nxt.add(e["source"])
            nxt -= keep_ids
            keep_ids |= nxt
            frontier = nxt
            if not frontier:
                break
        kg = {
            "nodes": [n for n in kg["nodes"] if n["id"] in keep_ids],
            "edges": [e for e in kg["edges"] if e["source"] in keep_ids and e["target"] in keep_ids],
        }
        notes.append(f"neighbourhood subgraph: {len(kg['nodes'])} nodes / {len(kg['edges'])} edges")

    if node_type:
        keep = {t.strip() for t in node_type.split(",") if t.strip()}
        unknown = keep - {n["type"] for n in kg["nodes"]}
        if unknown:
            notes.append(f"graph has no nodes of type: {', '.join(sorted(unknown))}")
        kept = [n for n in kg["nodes"] if n["type"] in keep]
        ids = {n["id"] for n in kept}
        # Both ends must survive or the edge is dropped, else dangling refs.
        kg = {"nodes": kept, "edges": [e for e in kg["edges"] if e["source"] in ids and e["target"] in ids]}
        notes.append(f"filtered to {len(kg['nodes'])} nodes / {len(kg['edges'])} edges")
        if kg["nodes"] and not kg["edges"]:
            notes.append(
                "no edges survived the filter — these types may not connect directly "
                "(e.g. Domain and InternalSystem both only connect to Repo); use "
                "around= for a connected subgraph, or include Repo in node_type"
            )

    if fmt not in _EXPORT_EXTENSIONS:
        return {"error": f"Unknown format {fmt!r} — choose from {', '.join(_EXPORT_EXTENSIONS)}"}
    out = Path(output) if output else (proj.root / f"kg/capability-map.{_EXPORT_EXTENSIONS[fmt]}")

    if fmt == "html":
        text = build_html(kg, title=proj.name, default_view=default_view)
    else:
        try:
            text = WRITERS[fmt](kg) if fmt == "dot" else WRITERS[fmt](kg, keep_sources)
        except ValueError as exc:
            return {"error": str(exc)}

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")

    return {
        "output_path": out,
        "size_bytes": out.stat().st_size,
        "nodes": len(kg["nodes"]),
        "edges": len(kg["edges"]),
        "format": fmt,
        "notes": notes,
        "tip": _EXPORT_TIPS[fmt],
        "kg": kg,  # post-filter graph, e.g. for view_summary() on the html path
    }
