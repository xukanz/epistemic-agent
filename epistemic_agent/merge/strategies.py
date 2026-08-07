"""Merge / dedup strategies.

Four strategies, each independent and composable: `AliasMerge` folds labels
onto the vocabulary term they ground to, `FuzzyLabelMerge` catches near-matches
that never grounded, `NaturalKeyGuard` reports (never fixes) violations of a
declared natural key, and `MultiTokenFlag` quarantines labels that pack more
than one value. `build_merge_map` / `apply_merges` compose whichever set a
project configures — see `strategies_from_config` — and merging repeats to a
fixed point (see `cli.py`'s `merge` command) since folding one alias can make
another node newly matchable.

A repo's `path` is a natural key: it must never collapse into another node
just because two labels look similar, which is why `NaturalKeyGuard` only
reports violations instead of merging them away — see that class's docstring.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime
from typing import Any

from epistemic_agent.kg.store import edge_id
from epistemic_agent.merge.canonical import (
    canonical_person_id,
    first_initial,
    name_parts,
    normalise_tech_label,
    slugify,
    split_multi_value,
)

try:
    from rapidfuzz import fuzz

    _HAS_RAPIDFUZZ = True
except ImportError:  # pragma: no cover
    _HAS_RAPIDFUZZ = False


def _prop(node: dict, key: str, default: str = "") -> str:
    return (node.get("properties") or {}).get(key, default) or default


def _label_of(node: dict) -> str:
    props = node.get("properties") or {}
    return props.get("label") or props.get("name") or node["id"]


# ---------------------------------------------------------------------------
# Base


class MergeStrategy(ABC):
    name: str = "strategy"

    @abstractmethod
    def run(self, kg: dict, merge: dict[str, str], notes: dict[str, str]) -> None:
        """Mutate merge and notes in place."""

    def canonical_props(self) -> dict[str, dict]:
        """Properties this strategy asserts on the nodes it merged into.

        A strategy that knows the authoritative name of a thing should say so;
        otherwise the surviving node keeps whichever alias happened to be
        created first, and the graph reads `tech-mongodb → "beanie"`. Populated
        during `run`, collected by `build_merge_map`, applied by `apply_merges`.
        """
        return {}


# ---------------------------------------------------------------------------
# 1. Alias folding against the controlled vocabulary


class AliasMerge(MergeStrategy):
    """Fold label variants onto the vocabulary term that owns them.

    This is the strategy that does most of the work on a technology corpus,
    because the messiness is lexical rather than semantic: `portkey-ai` and
    `Portkey` and `portkey 网关` are one thing written three ways, and the
    vocabulary already records that.

    Requires nodes to carry `term_id` (set during ingest grounding). Nodes that
    never grounded are left alone — guessing at them is what the review queue
    is for.
    """

    name = "alias"

    def __init__(self, node_type: str, id_prefix: str, vocabulary=None):
        self.node_type = node_type
        self.id_prefix = id_prefix
        self.vocabulary = vocabulary
        self._props: dict[str, dict] = {}

    def canonical_props(self) -> dict[str, dict]:
        return self._props

    def _canonical_id(self, term_id: str, group: list[dict]) -> str:
        """The canonical ID comes from the vocabulary term's ID.

        Two rejected alternatives, both tried on real data:

        * *Shortest node ID in the group* — absurd as soon as an alias is
          shorter than the canonical name: `mongodb` folded into `beanie`, and
          `httpx` into `axios`.
        * *Slug of the term's label* — labels are human-facing and may be in
          any language, so `重试与退避` slugified to the empty string and
          `HTTP 客户端` to `http`.

        Term IDs are ASCII, unique, and stable by construction. Use those, and
        the node ID stays readable and language-independent:
        ``dd:tenacity`` → ``tech-tenacity``.
        """
        if self.vocabulary is not None:
            term = self.vocabulary.term_by_id(term_id)
            if term:
                local = term_id.split(":", 1)[-1]
                slug = slugify(local)
                if slug:
                    return self.id_prefix + slug
        return sorted(group, key=lambda n: (len(n["id"]), n["id"]))[0]["id"]

    def run(self, kg: dict, merge: dict[str, str], notes: dict[str, str]) -> None:
        by_term: dict[str, list[dict]] = defaultdict(list)
        for n in kg["nodes"]:
            if n["type"] != self.node_type:
                continue
            term_id = _prop(n, "term_id")
            if term_id:
                by_term[term_id].append(n)

        for term_id, group in by_term.items():
            canonical_id = self._canonical_id(term_id, group)
            # The vocabulary owns the name. Assert it even for a group of one:
            # a lone node created from the alias `portkey-ai` should still end
            # up displaying as `Portkey`.
            if self.vocabulary is not None:
                term = self.vocabulary.term_by_id(term_id)
                if term:
                    self._props[canonical_id] = {
                        "label": term["label"],
                        "term_id": term_id,
                        "ontology_id": term["shard"],
                    }
            if len(group) < 2 and group[0]["id"] == canonical_id:
                continue
            for n in group:
                if n["id"] != canonical_id:
                    merge[n["id"]] = canonical_id
                    notes[n["id"]] = f"alias of {term_id} → {canonical_id}"


# ---------------------------------------------------------------------------
# 2. Fuzzy label matching for ungrounded nodes


class FuzzyLabelMerge(MergeStrategy):
    """Fuzzy-match ungrounded nodes against grounded ones of the same type.

    Merges an *ungrounded* node into a *grounded* one — a meaningful asymmetry:
    the grounded node has a vocabulary term behind it, so it wins.

    Comparisons are blocked by first character to keep this from being O(n²)
    across the whole graph, which is fine at 500 nodes and not at 50,000.
    """

    name = "fuzzy_label"

    def __init__(self, node_type: str, threshold: float = 0.92):
        if not _HAS_RAPIDFUZZ:
            raise ImportError("rapidfuzz is required for FuzzyLabelMerge")
        self.node_type = node_type
        self.threshold = threshold

    def run(self, kg: dict, merge: dict[str, str], notes: dict[str, str]) -> None:
        grounded: dict[str, list[dict]] = defaultdict(list)
        ungrounded: list[dict] = []
        for n in kg["nodes"]:
            if n["type"] != self.node_type:
                continue
            label = normalise_tech_label(_label_of(n))
            if not label:
                continue
            if _prop(n, "term_id"):
                grounded[label[0]].append(n)
            else:
                ungrounded.append(n)

        for n in ungrounded:
            label = normalise_tech_label(_label_of(n))
            if not label:
                continue
            best, best_score = None, 0.0
            for cand in grounded.get(label[0], []):
                score = fuzz.token_sort_ratio(label, normalise_tech_label(_label_of(cand))) / 100.0
                if score > best_score:
                    best, best_score = cand, score
            if best is not None and best_score >= self.threshold:
                merge[n["id"]] = best["id"]
                notes[n["id"]] = f"fuzzy label match {best_score:.2f} → grounded {best['id']}"


# ---------------------------------------------------------------------------
# 3. Natural-key guard


class NaturalKeyGuard(MergeStrategy):
    """Report — never silently fix — nodes that violate a natural-key invariant.

    A repo's `path` is globally unique. If two Repo nodes share one, the ingest
    produced them wrongly and a merge would hide the bug. Conversely, two repos
    with the same *name* under different namespaces are legitimately distinct,
    which is why the key is the path and not the name.

    Adds nothing to the merge map; findings surface in `notes` under the
    pseudo-key `!<strategy>:<key>` and are printed by `capmap merge`.
    """

    name = "natural_key"

    def __init__(self, node_type: str, key_prop: str):
        self.node_type = node_type
        self.key_prop = key_prop

    def run(self, kg: dict, merge: dict[str, str], notes: dict[str, str]) -> None:
        by_key: dict[str, list[str]] = defaultdict(list)
        for n in kg["nodes"]:
            if n["type"] != self.node_type:
                continue
            key = _prop(n, self.key_prop)
            if key:
                by_key[key].append(n["id"])
        for key, ids in by_key.items():
            if len(ids) > 1:
                notes[f"!{self.name}:{key}"] = (
                    f"{len(ids)} {self.node_type} nodes share {self.key_prop}={key!r}: "
                    f"{', '.join(sorted(ids))} — ingest bug, not a merge candidate"
                )


# ---------------------------------------------------------------------------
# 4. Multi-value token flag


class MultiTokenFlag(MergeStrategy):
    """Flag labels that carry several values ('anthropic/openai').

    Renames to a `-multi` suffix so the node is visibly quarantined, and leaves
    the actual split to a human — the review queue emits `emit_multi_token`.

    **Grounded nodes are exempt.** A node with a `term_id` has been resolved to
    a single vocabulary concept; a separator in its display name is part of
    that concept's name, not evidence of two things. Without this exemption the
    strategy fights `AliasMerge` forever over terms like `Jira / Confluence`:
    one quarantines the node, the other un-quarantines it, and the merge never
    reaches a fixed point.
    """

    name = "multi_token"

    def __init__(self, node_type: str):
        self.node_type = node_type

    def run(self, kg: dict, merge: dict[str, str], notes: dict[str, str]) -> None:
        for n in kg["nodes"]:
            if n["type"] != self.node_type or n["id"].endswith("-multi"):
                continue
            if _prop(n, "term_id"):
                continue
            # The node's own label, never `raw_label`: an ingest that already
            # split "boto3/Lambda" leaves the unsplit string in `raw_label` for
            # provenance, and re-reading it would quarantine both halves of a
            # correctly split pair.
            parts = split_multi_value(_label_of(n))
            if len(parts) > 1:
                new_id = n["id"] + "-multi"
                merge[n["id"]] = new_id
                notes[n["id"]] = (
                    f"multi-value token ({' + '.join(parts)}) — flagged for manual split"
                )


# ---------------------------------------------------------------------------
# 5. Person name resolution


class PersonNameMerge(MergeStrategy):
    """Resolve person nodes by (last name, first initial).

    Deliberately does not fold language-specific spelling variants (accents,
    transliterations) into this generic strategy — that is a property of one
    organisation's staff list, not of people in general. An instance that
    needs it passes `known_canonical`.
    """

    name = "person_name"

    def __init__(self, node_type: str = "Person",
                 known_canonical: dict[str, str] | None = None):
        self.node_type = node_type
        self.known_canonical = known_canonical or {}

    def run(self, kg: dict, merge: dict[str, str], notes: dict[str, str]) -> None:
        by_key: dict[tuple[str, str], str] = {}
        for n in kg["nodes"]:
            if n["type"] != self.node_type:
                continue
            first, last = name_parts(_prop(n, "name") or n["id"])
            if not last:
                continue
            key = (last, first_initial(first))
            if key in by_key and by_key[key] != n["id"]:
                target = min(by_key[key], n["id"], key=len)
                other = by_key[key] if target == n["id"] else n["id"]
                merge[other] = target
                notes[other] = f"same (last, initial) key {key} → {target}"
                by_key[key] = target
            else:
                by_key.setdefault(key, n["id"])

        for raw, canonical in self.known_canonical.items():
            nid = canonical_person_id(raw)
            if nid in {n["id"] for n in kg["nodes"]} and nid != canonical:
                merge[nid] = canonical
                notes[nid] = "resolved via instance canonical table"


# ---------------------------------------------------------------------------
# Config-driven construction

_STRATEGY_TYPES: dict[str, Any] = {
    "alias": AliasMerge,
    "fuzzy_label": FuzzyLabelMerge,
    "natural_key": NaturalKeyGuard,
    "multi_token": MultiTokenFlag,
    "person_name": PersonNameMerge,
}


def strategies_from_config(config: dict, vocabulary=None) -> list[MergeStrategy]:
    """Build the strategy list from `merge.strategies` in project.yaml.

    ``merge.strategies`` is a list of dicts, each with a `type` key plus that
    strategy's constructor arguments::

        merge:
          strategies:
            - {type: alias,       node_type: TechStack, id_prefix: "tech-"}
            - {type: fuzzy_label, node_type: TechStack, threshold: 0.93}
            - {type: natural_key, node_type: Repo,      key_prop: path}
            - {type: multi_token, node_type: TechStack}
    """
    out: list[MergeStrategy] = []
    for spec in (config.get("merge") or {}).get("strategies") or []:
        spec = dict(spec)
        kind = spec.pop("type", None)
        cls = _STRATEGY_TYPES.get(kind)
        if cls is None:
            raise ValueError(
                f"unknown merge strategy {kind!r}; known: {sorted(_STRATEGY_TYPES)}"
            )
        if cls is AliasMerge and vocabulary is not None:
            spec.setdefault("vocabulary", vocabulary)
        out.append(cls(**spec))
    return out


# ---------------------------------------------------------------------------
# Composer


def build_merge_map(
    kg: dict, strategies: list[MergeStrategy]
) -> tuple[dict[str, str], dict[str, str], dict[str, dict]]:
    """Returns (merge map, per-node notes, properties to assert on targets)."""
    merge: dict[str, str] = {}
    notes: dict[str, str] = {}
    props: dict[str, dict] = {}
    for s in strategies:
        s.run(kg, merge, notes)
        for node_id, values in s.canonical_props().items():
            props.setdefault(node_id, {}).update(values)

    def resolve(mid: str, depth: int = 0) -> str:
        if depth > 10 or mid not in merge:
            return mid
        return resolve(merge[mid], depth + 1)

    merge = {k: resolve(v) for k, v in merge.items() if resolve(v) != k}
    return merge, notes, props


def apply_merges(
    kg: dict,
    merge: dict[str, str],
    notes: dict[str, str],
    props: dict[str, dict] | None = None,
) -> dict:
    nodes_by_id = {n["id"]: n for n in kg["nodes"]}
    today = datetime.now().strftime("%Y-%m-%d")

    # A merge target that does not exist yet is a rename: clone the source.
    new_nodes = []
    for old_id, new_id in merge.items():
        if new_id not in nodes_by_id:
            old = nodes_by_id.get(old_id)
            if old is None:
                continue
            clone = json.loads(json.dumps(old))
            clone["id"] = new_id
            clone_props = clone.setdefault("properties", {})
            clone_props["_renamed_from"] = old_id
            clone_props["_last_updated"] = today
            new_nodes.append(clone)
            nodes_by_id[new_id] = clone

    # Carry over any property the target is missing.
    for old_id, new_id in merge.items():
        old = nodes_by_id.get(old_id)
        canon = nodes_by_id.get(new_id)
        if not old or not canon or old is canon:
            continue
        canon_props = canon.setdefault("properties", {})
        for k, v in (old.get("properties") or {}).items():
            if not canon_props.get(k) and v:
                canon_props[k] = v
        merged_from = canon_props.setdefault("_merged_from", [])
        if isinstance(merged_from, list) and old_id not in merged_from:
            merged_from.append(old_id)

    seen: set[tuple] = set()
    new_edges = []
    dropped_self_loops = 0
    dropped_dupes = 0
    for e in kg["edges"]:
        ne = dict(e)
        ne["source"] = merge.get(ne["source"], ne["source"])
        ne["target"] = merge.get(ne["target"], ne["target"])
        if ne["source"] == ne["target"]:
            dropped_self_loops += 1
            continue
        key = (ne["source"], ne["type"], ne["target"])
        if key in seen:
            dropped_dupes += 1
            continue
        seen.add(key)
        ne["id"] = edge_id(ne["source"], ne["type"], ne["target"])
        new_edges.append(ne)

    kg["nodes"] = [n for n in kg["nodes"] if n["id"] not in merge] + new_nodes

    # Strategy-asserted properties win over whatever survived the fold.
    relabelled = 0
    if props:
        by_id = {n["id"]: n for n in kg["nodes"]}
        for node_id, values in props.items():
            node = by_id.get(node_id)
            if node is None:
                continue
            node_props = node.setdefault("properties", {})
            for k, v in values.items():
                if v and node_props.get(k) != v:
                    node_props[k] = v
                    relabelled += 1

    kg["edges"] = new_edges
    kg["_merge_stats"] = {
        "merges": len(merge),
        "canonical_cloned": len(new_nodes),
        "edges_after": len(new_edges),
        "dropped_self_loops": dropped_self_loops,
        "dropped_duplicates": dropped_dupes,
        "invariant_violations": sum(1 for k in notes if k.startswith("!")),
        "properties_asserted": relabelled,
    }
    return kg


_ID_SUFFIX = re.compile(r"-(multi|inferred)$")


def strip_quarantine_suffix(node_id: str) -> str:
    return _ID_SUFFIX.sub("", node_id)


def run_merge(proj, dry_run: bool = False, report_path=None, max_rounds: int = 5) -> dict:
    """Run the merge strategies configured in `project.yaml` to a fixed point.

    Shared by `cli.py`'s `merge` command and the agent's `merge_dry_run` /
    `merge_apply` tools, so the fixed-point iteration (folding one alias can
    make another node newly matchable — see the module docstring) is
    implemented once. Returns a plain dict rather than printing, so callers
    decide how to present it (rich console, or a string handed back to an
    LLM tool call).
    """
    from pathlib import Path

    kg = proj.load_kg()
    before_nodes, before_edges = len(kg["nodes"]), len(kg["edges"])
    vocab = proj.vocabulary()

    if not strategies_from_config(proj.config, vocabulary=vocab):
        return {"error": "No merge strategies configured (merge.strategies in config/project.yaml)."}

    working = json.loads(json.dumps(kg)) if dry_run else kg
    all_merges: dict[str, str] = {}
    all_notes: dict[str, str] = {}
    mstats: dict = {}
    rounds = 0

    while rounds < max_rounds:
        strategies = strategies_from_config(proj.config, vocabulary=vocab)
        merge_map, notes, canonical_props = build_merge_map(working, strategies)
        all_notes.update(notes)
        if not merge_map:
            break
        rounds += 1
        for old, new in list(all_merges.items()):
            if new in merge_map:
                all_merges[old] = merge_map[new]
        all_merges.update(merge_map)
        all_merges = {k: v for k, v in all_merges.items() if k != v}
        working = apply_merges(working, merge_map, notes, canonical_props)
        round_stats = working.pop("_merge_stats", {})
        for k, v in round_stats.items():
            mstats[k] = mstats.get(k, 0) + v if isinstance(v, int) else v

    violations = {k: v for k, v in all_notes.items() if k.startswith("!")}

    report_path = Path(report_path) if report_path else (proj.root / "kg" / "merge-report.md")
    lines = [
        "# KG merge report",
        "",
        f"- Nodes: {before_nodes} → {len(working['nodes'])}",
        f"- Edges: {before_edges} → {len(working['edges'])}",
        f"- Merges: {len(all_merges)} over {rounds} round(s)",
        f"- Invariant violations: {len(violations)}",
        "",
    ]
    if violations:
        lines += ["## Invariant violations (not merged — fix the ingest)", ""]
        lines += [f"- {v}" for v in violations.values()]
        lines += [""]
    lines += ["## Merge map", "", "| old | new | reason |", "|---|---|---|"]
    for old, new in sorted(all_merges.items(), key=lambda x: (x[1], x[0])):
        lines.append(f"| `{old}` | `{new}` | {all_notes.get(old, '')} |")
    report_text = "\n".join(lines) + "\n"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text)

    if not dry_run:
        proj.kg_path.write_text(json.dumps(working, indent=2, ensure_ascii=False) + "\n")

    return {
        "dry_run": dry_run,
        "before_nodes": before_nodes,
        "before_edges": before_edges,
        "after_nodes": len(working["nodes"]),
        "after_edges": len(working["edges"]),
        "merges": len(all_merges),
        "rounds": rounds,
        "hit_max_rounds": rounds >= max_rounds,
        "violations": len(violations),
        "merge_stats": mstats,
        "report_path": report_path,
        "report_text": report_text,
    }
