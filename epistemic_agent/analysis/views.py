"""The five questions a technology capability map exists to answer.

Management asking "who here can do X, and are two teams building the same
thing" wants a table, not a document to read end to end — so the analysis
layer is a set of deterministic views over the graph, not a narrative
generator. Each view returns plain data; rendering lives in `render_*`. That
split keeps the views usable from a notebook or an agent, not just the CLI.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any


def _label(node: dict) -> str:
    props = node.get("properties") or {}
    return props.get("label") or props.get("name") or node["id"]


def _prop(node: dict, key: str, default=""):
    return (node.get("properties") or {}).get(key, default) or default


class GraphIndex:
    """Precomputed lookups. Built once, shared by every view."""

    def __init__(self, kg: dict):
        self.nodes = {n["id"]: n for n in kg.get("nodes", [])}
        self.edges = kg.get("edges", [])
        self.by_type: dict[str, list[dict]] = defaultdict(list)
        for n in self.nodes.values():
            self.by_type[n["type"]].append(n)

        self.out: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        self.inb: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        for e in self.edges:
            self.out[e["type"]][e["source"]].add(e["target"])
            self.inb[e["type"]][e["target"]].add(e["source"])

        self.repo_team: dict[str, str] = {}
        for repo, teams in self.out["OWNED_BY"].items():
            if teams:
                self.repo_team[repo] = sorted(teams)[0]

    def label(self, node_id: str) -> str:
        n = self.nodes.get(node_id)
        return _label(n) if n else node_id

    def teams_for(self, target_id: str, edge_types=("USES_TECH", "DEMONSTRATES", "IMPLEMENTS")
                  ) -> set[str]:
        teams: set[str] = set()
        for et in edge_types:
            for repo in self.inb[et].get(target_id, ()):
                t = self.repo_team.get(repo)
                if t:
                    teams.add(t)
        return teams

    def repos_for(self, target_id: str, edge_types=("USES_TECH", "DEMONSTRATES", "IMPLEMENTS")
                  ) -> set[str]:
        repos: set[str] = set()
        for et in edge_types:
            repos |= self.inb[et].get(target_id, set())
        return repos


# ---------------------------------------------------------------------------
# 1. Coverage — what does this organisation actually do?


def view_coverage(idx: GraphIndex, by: str = "shard") -> list[dict]:
    """Capability coverage grouped by vocabulary shard (or by domain).

    `by="shard"` answers "which areas of the stack are we deep in".
    `by="domain"` answers "which parts of the business have agent work".
    """
    rows: list[dict] = []
    if by == "domain":
        for d in idx.by_type.get("Domain", []):
            repos = idx.inb["IN_DOMAIN"].get(d["id"], set())
            techs: set[str] = set()
            teams: set[str] = set()
            for r in repos:
                techs |= idx.out["USES_TECH"].get(r, set())
                if r in idx.repo_team:
                    teams.add(idx.repo_team[r])
            rows.append(
                {
                    "group": _label(d),
                    "repos": len(repos),
                    "teams": len(teams),
                    "distinct_tech": len(techs),
                }
            )
        rows.sort(key=lambda r: -r["repos"])
        return rows

    shard_tech: dict[str, list[dict]] = defaultdict(list)
    for t in idx.by_type.get("TechStack", []):
        shard_tech[_prop(t, "ontology_id") or "(ungrounded)"].append(t)

    for shard, techs in shard_tech.items():
        repos: set[str] = set()
        teams: set[str] = set()
        for t in techs:
            repos |= idx.inb["USES_TECH"].get(t["id"], set())
        for r in repos:
            if r in idx.repo_team:
                teams.add(idx.repo_team[r])
        top = sorted(
            techs, key=lambda t: -len(idx.inb["USES_TECH"].get(t["id"], set()))
        )[:5]
        rows.append(
            {
                "group": shard,
                "distinct_tech": len(techs),
                "repos": len(repos),
                "teams": len(teams),
                "top": [
                    f"{_label(t)}({len(idx.inb['USES_TECH'].get(t['id'], set()))})" for t in top
                ],
            }
        )
    rows.sort(key=lambda r: -r["repos"])
    return rows


# ---------------------------------------------------------------------------
# 2. Duplicates — is anyone building the same thing twice?


def view_duplicates(
    idx: GraphIndex, min_shared: int = 3, min_jaccard: float = 0.5, cross_team_only: bool = True
) -> list[dict]:
    """Repos in one domain with heavily overlapping stacks.

    `cross_team_only` defaults to True because two repos inside one team are
    usually the same team's own iterations, which is not the finding anyone
    wants surfaced. Duplication across team boundaries is the expensive kind.

    Each row carries a `relation`. On the first real corpus this view was run
    against, every one of the top ten hits was a fork or a workshop copy —
    identical stacks because they are literally the same repository, which is
    not duplicated effort. Separating those out is what makes the rest of the
    list worth reading.
    """
    stacks = {r: set(t) for r, t in idx.out["USES_TECH"].items()}
    out: list[dict] = []
    for domain, repos in idx.inb["IN_DOMAIN"].items():
        eligible = sorted(r for r in repos if len(stacks.get(r, ())) >= min_shared)
        for i, a in enumerate(eligible):
            for b in eligible[i + 1:]:
                ta, tb = idx.repo_team.get(a), idx.repo_team.get(b)
                if cross_team_only and ta is not None and ta == tb:
                    continue
                sa, sb = stacks[a], stacks[b]
                inter, union = sa & sb, sa | sb
                if not union:
                    continue
                j = len(inter) / len(union)
                if j >= min_jaccard and len(inter) >= min_shared:
                    out.append(
                        {
                            "domain": idx.label(domain),
                            "repo_a": a,
                            "repo_b": b,
                            "label_a": idx.label(a),
                            "label_b": idx.label(b),
                            "team_a": idx.label(ta) if ta else "?",
                            "team_b": idx.label(tb) if tb else "?",
                            "shared": sorted(idx.label(t) for t in inter),
                            "jaccard": round(j, 3),
                            "relation": _relation(idx, a, b, j),
                        }
                    )
    # Genuinely independent builds first — those are the ones worth a
    # conversation. Forks and copies stay in the list, but at the bottom.
    order = {"independent": 0, "same-name": 1, "fork-or-copy": 2}
    out.sort(key=lambda d: (order.get(d["relation"], 0), -d["jaccard"], -len(d["shared"])))
    return out


def _repo_slug(idx: GraphIndex, node_id: str) -> str:
    """Last path segment, normalised.

    The comparison has to be on the *path*, not on the display name. Five forks
    of one workshop template were labelled "Copilot Hackathon SSF (Preet)",
    "(Diana)", "(workshop)" … — different names, identical
    `.../github-copilot-hackathon-ssf` paths. Names are what humans wrote about
    a repo; the path is what the repo is.
    """
    node = idx.nodes.get(node_id, {})
    path = (_prop(node, "path") or "").strip().lower()
    if path:
        return re.sub(r"[^a-z0-9]+", "-", path.rsplit("/", 1)[-1]).strip("-")
    return (_prop(node, "name") or "").strip().lower()


def _relation(idx: GraphIndex, a: str, b: str, jaccard: float) -> str:
    """Cheap discrimination between 'same repo, copied' and 'built twice'."""
    sa, sb = _repo_slug(idx, a), _repo_slug(idx, b)
    if sa and sa == sb:
        return "fork-or-copy" if jaccard >= 0.99 else "same-name"
    if jaccard >= 0.99 and sa and sb and (sa in sb or sb in sa):
        return "fork-or-copy"
    return "independent"


# ---------------------------------------------------------------------------
# 3. Gaps — what does nobody here do?


def view_gaps(idx: GraphIndex, vocabulary, include_ungrounded: bool = False) -> list[dict]:
    """Two distinct findings, ordered so the stronger one comes first.

    * `no-repo` — a vocabulary term nothing in the graph maps to. The closest
      thing to "nobody here does this", subject to the vocabulary being
      complete and the ingest having read everything.
    * `single-repo` — exactly one repo. No redundancy, no internal comparison.

    *Ungrounded* single-repo labels are excluded by default. A one-off string
    the vocabulary does not recognise is a vocabulary suggestion — it is
    already accumulating in `kg/vocabulary-suggestions.md` — and letting a few
    hundred of them into this view buries the real gaps under noise.
    """
    covered = {
        _prop(n, "term_id")
        for n in idx.nodes.values()
        if _prop(n, "term_id")
    }
    thin: list[dict] = []
    ungrounded_skipped = 0
    for t in idx.by_type.get("TechStack", []):
        repos = idx.inb["USES_TECH"].get(t["id"], set())
        if len(repos) != 1:
            continue
        term_id = _prop(t, "term_id")
        if not term_id:
            ungrounded_skipped += 1
            if not include_ungrounded:
                continue
        thin.append(
            {
                "term_id": term_id or "-",
                "label": _label(t),
                "shard": _prop(t, "ontology_id") or "(ungrounded)",
                "status": "single-repo",
                "repos": 1,
            }
        )
    # A vocabulary term can only be called a gap if the extraction that would
    # have found it actually ran. If the graph holds *no* nodes of a term's
    # target type, every term of that type would report `no-repo` — which reads
    # as "nobody here does RAG" when it means "the pattern extractor has not
    # been run". Skip those types and say so instead.
    populated_types = {t for t, ns in idx.by_type.items() if ns}
    missing = [
        {
            "term_id": term["term_id"],
            "label": term["label"],
            "shard": term["shard"],
            "status": "no-repo",
            "repos": 0,
        }
        for term in vocabulary._terms  # noqa: SLF001
        if term["term_id"] not in covered and term["target_type"] in populated_types
    ]
    missing.sort(key=lambda r: (r["shard"], r["label"]))
    thin.sort(key=lambda r: (r["shard"], r["label"]))
    return missing + thin


def count_ungrounded_singletons(idx: GraphIndex) -> int:
    """How many one-off unrecognised labels `view_gaps` left out."""
    return sum(
        1
        for t in idx.by_type.get("TechStack", [])
        if not _prop(t, "term_id") and len(idx.inb["USES_TECH"].get(t["id"], set())) == 1
    )


def unpopulated_target_types(idx: GraphIndex, vocabulary) -> dict[str, int]:
    """Vocabulary target types with no nodes at all, and their term counts.

    Reported alongside `view_gaps` so that a whole missing extraction pass is
    visible as such, rather than silently disappearing from the gap list.
    """
    populated = {t for t, ns in idx.by_type.items() if ns}
    out: dict[str, int] = defaultdict(int)
    for term in vocabulary._terms:  # noqa: SLF001
        if term["target_type"] not in populated:
            out[term["target_type"]] += 1
    return dict(out)


# ---------------------------------------------------------------------------
# 4. Experts — who here has done X?


def view_experts(idx: GraphIndex, query: str, vocabulary=None, limit: int = 20) -> dict[str, Any]:
    """Resolve a free-text query to a graph node, then list who touched it.

    The vocabulary is used to resolve the query so that asking for "k8s" finds
    the Kubernetes node — the same grounding that built the graph also reads it.
    """
    target_ids: list[str] = []
    resolved_label = query

    if vocabulary is not None:
        res = vocabulary.ground(query)
        if res.best is not None:
            resolved_label = res.best.label
            target_ids = [
                n["id"]
                for n in idx.nodes.values()
                if _prop(n, "term_id") == res.best.term_id
            ]

    if not target_ids:
        q = query.lower()
        target_ids = [
            n["id"]
            for n in idx.nodes.values()
            if n["type"] in ("TechStack", "Capability", "Pattern")
            and q in _label(n).lower()
        ]

    hits: list[dict] = []
    for tid in target_ids:
        for repo in sorted(idx.repos_for(tid)):
            node = idx.nodes.get(repo, {})
            team = idx.repo_team.get(repo)
            hits.append(
                {
                    "repo": repo,
                    "label": idx.label(repo),
                    "path": _prop(node, "path"),
                    "team": idx.label(team) if team else "?",
                    "tier": _prop(node, "tier"),
                    "reuse": _prop(node, "reuse"),
                    "last_commit": _prop(node, "last_commit"),
                    "value": _prop(node, "value"),
                }
            )
    # Most recently active first — a 2023 repo is a weaker answer to "who can
    # help me with this" than a repo someone touched last month.
    hits.sort(key=lambda h: (h["last_commit"] or "", h["tier"] or "Z"), reverse=True)
    return {
        "query": query,
        "resolved": resolved_label,
        "matched_nodes": target_ids,
        "hits": hits[:limit],
        "total": len(hits),
    }


# ---------------------------------------------------------------------------
# 5. Risk — where is this organisation fragile?


def view_risk(idx: GraphIndex, stale_before: str = "") -> dict[str, Any]:
    single_team: list[dict] = []
    for t in idx.by_type.get("TechStack", []) + idx.by_type.get("Pattern", []):
        repos = idx.repos_for(t["id"])
        teams = idx.teams_for(t["id"])
        if len(teams) == 1 and repos:
            single_team.append(
                {
                    "id": t["id"],
                    "label": _label(t),
                    "team": idx.label(next(iter(teams))),
                    "repos": len(repos),
                }
            )
    single_team.sort(key=lambda d: -d["repos"])

    lockin: list[dict] = []
    for s in idx.by_type.get("InternalSystem", []):
        repos = idx.inb["DEPENDS_ON_INTERNAL"].get(s["id"], set())
        teams = {idx.repo_team[r] for r in repos if r in idx.repo_team}
        if repos:
            lockin.append(
                {
                    "id": s["id"],
                    "label": _label(s),
                    "repos": len(repos),
                    "teams": len(teams),
                }
            )
    lockin.sort(key=lambda d: -d["repos"])

    stale: list[dict] = []
    if stale_before:
        for r in idx.by_type.get("Repo", []):
            lc = _prop(r, "last_commit")
            if lc and lc < stale_before:
                team = idx.repo_team.get(r["id"])
                stale.append(
                    {
                        "id": r["id"],
                        "label": _label(r),
                        "team": idx.label(team) if team else "?",
                        "last_commit": lc,
                        "tier": _prop(r, "tier"),
                    }
                )
        stale.sort(key=lambda d: d["last_commit"])

    return {"single_team": single_team, "internal_lockin": lockin, "stale_repos": stale}


# ---------------------------------------------------------------------------
# Rendering


def render_table(console, title: str, columns: list[str], rows: list[list[str]],
                 limit: int | None = None) -> None:
    from rich.table import Table

    shown = rows if limit is None else rows[:limit]
    t = Table(title=title, title_justify="left")
    for c in columns:
        t.add_column(c, overflow="fold")
    for r in shown:
        t.add_row(*[str(x) for x in r])
    console.print(t)
    if limit is not None and len(rows) > limit:
        console.print(f"[dim]… {len(rows) - limit} more rows (use --limit 0 for all)[/dim]")
