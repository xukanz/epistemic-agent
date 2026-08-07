"""Controlled-vocabulary grounding for technology labels.

The interface is `find_terms` / `find_concepts` returning `ConceptCluster`
objects with a `cluster_score`, routed through three confidence bands (0.70 /
0.50 — see `AUTO_GROUND_THRESHOLD` / `CANDIDATE_THRESHOLD` below). Ingest calls
this to resolve a free-text label like `"k8s"` to a canonical term before
writing a node.

**Why in-process rather than a separate MCP server.** There is no external
authority to defer to for a technology-stack vocabulary the way EDAM/GO/MONDO
are authorities for biomedical ontologies: it is small (hundreds of terms),
organisation-specific, and has to be maintained by the same people who
maintain the map. Standing up a second process to serve a few YAML files would
buy nothing. The seam is kept, though — see `resolve_backend` — so an org that
does have a shared internal tech taxonomy can put it behind MCP without
touching the ingest code.

Shard format (`shards/*.yaml`)::

    shard: agent-framework
    label: Agent 框架与编排
    terms:
      - id: af:langgraph
        label: LangGraph
        aliases: [langgraph, langgraph-checkpoint-sqlite]
        parent: af:agent-orchestration   # optional → SPECIALIZES edge
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

import yaml

from epistemic_agent.merge.canonical import normalise_tech_label

try:
    from rapidfuzz import fuzz

    _HAS_RAPIDFUZZ = True
except ImportError:  # pragma: no cover - rapidfuzz is a declared dependency
    _HAS_RAPIDFUZZ = False

# Routing bands. See the module docstring for what each one means.
AUTO_GROUND_THRESHOLD = 0.70
CANDIDATE_THRESHOLD = 0.50

DEFAULT_SHARD_DIR = Path(__file__).parent / "shards"


@dataclass
class ConceptCluster:
    """One candidate mapping of a free-text label to a vocabulary term."""

    term_id: str
    label: str
    shard: str
    score: float
    parent: str | None = None
    matched_on: str = ""

    def as_dict(self) -> dict:
        return {
            "term_id": self.term_id,
            "label": self.label,
            "ontology_id": self.shard,
            "score": round(self.score, 3),
            "parent": self.parent,
            "matched_on": self.matched_on,
        }


@dataclass
class GroundingResult:
    """Outcome of the three-band routing decision."""

    status: str  # grounded | candidate | ungrounded
    query: str
    best: ConceptCluster | None = None
    candidates: list[ConceptCluster] = field(default_factory=list)

    @property
    def score(self) -> float:
        return self.best.score if self.best else 0.0


class VocabularyBackend(Protocol):
    def find_terms(self, query: str, shard: str | None = None, limit: int = 5
                   ) -> list[ConceptCluster]: ...

    def find_concepts(self, label: str, limit: int = 5) -> list[ConceptCluster]: ...


# ---------------------------------------------------------------------------
# Local YAML-backed vocabulary


class LocalVocabulary:
    """Loads YAML shards and resolves labels against them.

    Resolution order, highest confidence first:

    1. exact match on the normalised canonical label   → 1.00
    2. exact match on a normalised alias               → 0.98
    3. one is a whole-token prefix/suffix of the other → 0.80
    4. rapidfuzz token_sort_ratio                      → ratio / 100

    Step 2 scores just below step 1 so that when a token matches one term's
    canonical name and another term's alias, the canonical wins. That matters
    for pairs like `langchain` (canonical) vs `langchain-openai` (alias of
    LangChain but also arguably its own thing).
    """

    def __init__(self, shard_dirs: Iterable[Path] | None = None):
        self.shard_dirs = [Path(d) for d in (shard_dirs or [DEFAULT_SHARD_DIR])]
        self.shards: dict[str, dict] = {}
        self._terms: list[dict] = []
        # (target_type, normalised text) -> (term, how). Keyed by target type
        # because the same string means different things to different node
        # types: "react" is a frontend framework to a TechStack node and the
        # ReAct loop to a Pattern node.
        self._exact: dict[tuple[str, str], tuple[dict, str]] = {}
        self._load()

    # -- loading -----------------------------------------------------------

    def _load(self) -> None:
        for d in self.shard_dirs:
            if not d.exists():
                continue
            for path in sorted(d.glob("*.yaml")):
                data = yaml.safe_load(path.read_text()) or {}
                shard = data.get("shard") or path.stem
                target_type = data.get("target_type", "TechStack")
                self.shards[shard] = {
                    "shard": shard,
                    "label": data.get("label", shard),
                    "target_type": target_type,
                    "description": (data.get("description") or "").strip(),
                    "path": str(path),
                    "term_count": len(data.get("terms") or []),
                }
                for term in data.get("terms") or []:
                    entry = {
                        "term_id": term["id"],
                        "label": term["label"],
                        "shard": shard,
                        "target_type": target_type,
                        "parent": term.get("parent"),
                        "aliases": list(term.get("aliases") or []),
                    }
                    self._terms.append(entry)
                    canon = normalise_tech_label(entry["label"])
                    key = (target_type, canon)
                    self._exact.setdefault(key, (entry, "label"))
                    for alias in entry["aliases"]:
                        na = normalise_tech_label(alias)
                        if (target_type, na) not in self._exact:
                            self._exact[(target_type, na)] = (entry, "alias")

    # -- query -------------------------------------------------------------

    def list_shards(self) -> list[dict]:
        return sorted(self.shards.values(), key=lambda s: s["shard"])

    @property
    def term_count(self) -> int:
        return len(self._terms)

    def alias_map(self, target_type: str = "TechStack") -> dict[str, str]:
        """normalised text -> term_id. Used by AliasMerge as a merge table."""
        return {
            text: entry["term_id"]
            for (ttype, text), (entry, _) in self._exact.items()
            if ttype == target_type
        }

    def term_by_id(self, term_id: str) -> dict | None:
        for t in self._terms:
            if t["term_id"] == term_id:
                return t
        return None

    def find_terms(
        self,
        query: str,
        shard: str | None = None,
        limit: int = 5,
        target_type: str = "TechStack",
    ) -> list[ConceptCluster]:
        """Single-shard retrieval (or all shards of `target_type` when None)."""
        q = normalise_tech_label(query)
        if not q:
            return []

        scored: dict[str, ConceptCluster] = {}

        def offer(entry: dict, score: float, how: str) -> None:
            if shard is not None and entry["shard"] != shard:
                return
            if target_type is not None and entry["target_type"] != target_type:
                return
            prev = scored.get(entry["term_id"])
            if prev is None or score > prev.score:
                scored[entry["term_id"]] = ConceptCluster(
                    term_id=entry["term_id"],
                    label=entry["label"],
                    shard=entry["shard"],
                    score=score,
                    parent=entry.get("parent"),
                    matched_on=how,
                )

        hit = self._exact.get((target_type, q))
        if hit is not None:
            entry, how = hit
            offer(entry, 1.0 if how == "label" else 0.98, f"exact:{how}")

        q_tokens = set(_tokens(q))
        for entry in self._terms:
            if target_type is not None and entry["target_type"] != target_type:
                continue
            surfaces = [entry["label"], *entry["aliases"]]
            best = 0.0
            best_how = ""
            for surface in surfaces:
                ns = normalise_tech_label(surface)
                if not ns:
                    continue
                if ns == q:
                    continue  # already handled by the exact table
                s_tokens = set(_tokens(ns))
                if q_tokens and s_tokens and (q_tokens <= s_tokens or s_tokens <= q_tokens):
                    cand, how = 0.80, "token-subset"
                elif _HAS_RAPIDFUZZ:
                    cand = fuzz.token_sort_ratio(q, ns) / 100.0
                    how = "fuzzy"
                else:
                    cand, how = (1.0, "exact") if q == ns else (0.0, "")
                if cand > best:
                    best, best_how = cand, how
            if best > 0:
                offer(entry, best, best_how)

        out = sorted(scored.values(), key=lambda c: (-c.score, c.term_id))
        return out[:limit]

    def find_concepts(
        self, label: str, limit: int = 5, target_type: str = "TechStack"
    ) -> list[ConceptCluster]:
        """Cross-shard aggregation: search every shard of `target_type` at once."""
        return self.find_terms(label, shard=None, limit=limit, target_type=target_type)

    # -- routing -----------------------------------------------------------

    def ground(self, label: str, limit: int = 3, target_type: str = "TechStack"
               ) -> GroundingResult:
        """Apply the three-band routing decision to a free-text label."""
        clusters = self.find_concepts(label, limit=max(limit, 3), target_type=target_type)
        if not clusters:
            return GroundingResult(status="ungrounded", query=label)
        best = clusters[0]
        if best.score >= AUTO_GROUND_THRESHOLD:
            return GroundingResult(status="grounded", query=label, best=best,
                                   candidates=clusters[:limit])
        if best.score >= CANDIDATE_THRESHOLD:
            return GroundingResult(status="candidate", query=label, best=best,
                                   candidates=clusters[:limit])
        return GroundingResult(status="ungrounded", query=label, candidates=clusters[:limit])


def _tokens(s: str) -> list[str]:
    return [t for t in s.replace("/", " ").replace("-", " ").replace(".", " ").split() if t]


# ---------------------------------------------------------------------------
# Backend seam


class McpVocabulary:
    """Placeholder for an MCP-served vocabulary.

    Left deliberately unimplemented rather than removed: an organisation with a
    shared internal technology taxonomy should be able to swap the backend
    without touching ingest. Implement `find_terms` / `find_concepts` against
    your MCP server and register it in `resolve_backend`; the routing bands and
    everything downstream stay as they are.
    """

    def __init__(self, server: str):
        self.server = server

    def find_terms(self, query: str, shard: str | None = None, limit: int = 5
                   ) -> list[ConceptCluster]:
        raise NotImplementedError(
            "MCP vocabulary backend is not implemented. Set onto.backend: local in "
            "config/project.yaml, or implement McpVocabulary against your server."
        )

    def find_concepts(self, label: str, limit: int = 5) -> list[ConceptCluster]:
        return self.find_terms(label, limit=limit)


def resolve_backend(config: dict | None = None, project_root: Path | None = None):
    """Build the vocabulary backend named by `onto:` in the project config.

    ``onto.extra_shard_dirs`` lets an instance add its own shards — the place
    for organisation-specific vocabulary such as an internal-systems list —
    without editing the framework.
    """
    cfg = (config or {}).get("onto") or {}
    backend = cfg.get("backend", "local")

    if backend == "mcp":
        return McpVocabulary(server=cfg.get("mcp_server", "techonto"))

    dirs: list[Path] = [DEFAULT_SHARD_DIR]
    for extra in cfg.get("extra_shard_dirs") or []:
        p = Path(extra)
        if not p.is_absolute() and project_root:
            p = Path(project_root) / p
        dirs.append(p)
    return LocalVocabulary(shard_dirs=dirs)
