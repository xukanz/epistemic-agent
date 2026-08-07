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
