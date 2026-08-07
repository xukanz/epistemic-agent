"""Deterministic clustering of ungrounded labels into a vocabulary draft.

`kg/vocabulary-suggestions.md` (and a fresh bootstrap payload, before any
vocabulary exists to ground against) already carry every raw label a human
would otherwise transcribe by hand into `vocabulary/*.yaml`. What is missing is
grouping: `Portkey`, `portkey-ai`, `Portkey AI` are the same term wearing three
spellings. That grouping is a string problem and belongs in code — deciding
whether the resulting term is a real organisation system, worth a dedicated
entry, or noise is a judgement call and belongs to `skills/seed-vocabulary.md`.

Two clustering layers, both string-only, no LLM:

1. **Exact** — labels whose `normalise_tech_label` form is identical.
2. **Fuzzy** (optional) — remaining distinct normalised forms merged by
   `rapidfuzz.fuzz.token_sort_ratio` above `fuzzy_threshold`. Hostnames are
   excluded from this layer entirely: `sso.acme.example` and `app.acme.example`
   share two of three dot-segments and would otherwise merge, which is exactly
   the mistake `docs/new-instance.md` calls out under "主机名不要做模糊匹配".

This module only ever writes *draft* files outside `vocabulary/` — never the
real shards. `LocalVocabulary` glob-loads every `*.yaml` under an
`extra_shard_dirs` entry, so a draft written there would get treated as ground
truth before anyone reviewed it.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import yaml

from epistemic_agent.merge.canonical import normalise_tech_label, slugify

try:
    from rapidfuzz import fuzz

    _HAS_RAPIDFUZZ = True
except ImportError:  # pragma: no cover - rapidfuzz is a declared dependency
    _HAS_RAPIDFUZZ = False

DEFAULT_FUZZY_THRESHOLD = 0.90

# node type -> shard file stem under the draft output dir, and the
# `target_type` the resulting shard should declare (mirrors
# `ingest/document.py`'s DEFAULT_GROUNDABLE: InternalSystem and Capability
# both ground against TechStack shards).
_SHARD_FOR_TYPE = {
    "TechStack": ("techstack", "TechStack"),
    "InternalSystem": ("internal-system", "TechStack"),
    "Capability": ("capability", "TechStack"),
    "Pattern": ("pattern", "Pattern"),
}

_HOSTNAME_RE = re.compile(
    r"^(?:[a-z0-9]([a-z0-9-]*[a-z0-9])?\.){2,}[a-z]{2,}$", re.IGNORECASE
)

_SUGGESTION_LINE_RE = re.compile(r"^-\s+`(?P<label>[^`]+)`\s+—\s+(?P<type>\S+)\s+\((?P<node_id>[^)]+)\)")


def looks_like_hostname(label: str) -> bool:
    s = label.strip()
    if "://" in s:
        return True
    return bool(_HOSTNAME_RE.match(s))


# ---------------------------------------------------------------------------
# Input adapters


def labels_from_payload(payload: dict) -> list[dict]:
    """Pull groundable-type labels straight from a bootstrap payload — the
    main use case: a brand-new instance whose vocabulary is still empty, so
    every TechStack/InternalSystem node bootstrap just produced is a gap.
    """
    out = []
    for node in payload.get("nodes", []):
        ntype = node.get("type")
        if ntype not in _SHARD_FOR_TYPE:
            continue
        label = node.get("label") or node.get("raw_label")
        if not label:
            continue
        out.append({"label": label, "type": ntype, "node_id": node.get("id", "")})
    return out


def labels_from_suggestions_md(text: str) -> list[dict]:
    """Parse `kg/vocabulary-suggestions.md`'s `- \\`label\\` — Type (node_id)`
    lines — the accumulated-gap view for an instance that already has some
    vocabulary but keeps finding more.
    """
    out = []
    for line in text.splitlines():
        m = _SUGGESTION_LINE_RE.match(line.strip())
        if m:
            out.append(
                {"label": m.group("label"), "type": m.group("type"), "node_id": m.group("node_id")}
            )
    return out


# ---------------------------------------------------------------------------
# Clustering


class _UnionFind:
    def __init__(self, items: Iterable[str]):
        self.parent = {i: i for i in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _cluster_one_type(
    entries: list[dict], fuzzy_threshold: float | None
) -> list[dict]:
    """entries: [{label, node_id}] all of the same target type.

    Returns a list of clusters: [{canonical, aliases: set[str], raw: set[str],
    node_ids: set[str], hostname: bool}].
    """
    # Exact layer: group by normalised form.
    groups: dict[str, dict] = {}
    for e in entries:
        norm = normalise_tech_label(e["label"])
        if not norm:
            continue
        g = groups.setdefault(
            norm,
            {"raw": set(), "node_ids": set(), "hostname": False},
        )
        g["raw"].add(e["label"])
        if e.get("node_id"):
            g["node_ids"].add(e["node_id"])
        if looks_like_hostname(e["label"]):
            g["hostname"] = True

    keys = list(groups)
    uf = _UnionFind(keys)

    if fuzzy_threshold is not None and _HAS_RAPIDFUZZ:
        fuzzy_keys = [k for k in keys if not groups[k]["hostname"]]
        for i, a in enumerate(fuzzy_keys):
            for b in fuzzy_keys[i + 1 :]:
                if fuzz.token_sort_ratio(a, b) / 100.0 >= fuzzy_threshold:
                    uf.union(a, b)

    merged: dict[str, dict] = {}
    for k in keys:
        root = uf.find(k)
        m = merged.setdefault(root, {"raw": set(), "node_ids": set(), "hostname": False})
        m["raw"] |= groups[k]["raw"]
        m["node_ids"] |= groups[k]["node_ids"]
        m["hostname"] = m["hostname"] or groups[k]["hostname"]

    clusters = []
    for m in merged.values():
        canonical = max(m["raw"], key=lambda s: (len(s), s))
        aliases = sorted({normalise_tech_label(r) for r in m["raw"]} - {normalise_tech_label(canonical)})
        clusters.append(
            {
                "canonical": canonical,
                "aliases": aliases,
                "raw": sorted(m["raw"]),
                "node_ids": sorted(m["node_ids"]),
                "hostname": m["hostname"],
            }
        )
    return sorted(clusters, key=lambda c: c["canonical"].lower())


def build_vocab_draft(
    labels: list[dict], fuzzy_threshold: float | None = DEFAULT_FUZZY_THRESHOLD
) -> dict[str, dict]:
    """Cluster `labels` (as produced by the input adapters above) into a
    per-node-type draft vocabulary shard.

    Returns ``{node_type: {"shard": ..., "target_type": ..., "label": ...,
    "terms": [...], "clusters": n, "raw_labels": n}}`` — one entry per node
    type present in the input. `fuzzy_threshold=None` disables the fuzzy
    layer entirely (exact clustering only).
    """
    by_type: dict[str, list[dict]] = {}
    for e in labels:
        if e["type"] not in _SHARD_FOR_TYPE:
            continue
        by_type.setdefault(e["type"], []).append(e)

    drafts: dict[str, dict] = {}
    for ntype, entries in by_type.items():
        shard_name, target_type = _SHARD_FOR_TYPE[ntype]
        clusters = _cluster_one_type(entries, fuzzy_threshold)
        terms = []
        for c in clusters:
            terms.append(
                {
                    "id": f"{shard_name}-draft:{slugify(c['canonical'])}",
                    "label": c["canonical"],
                    "aliases": c["aliases"],
                    "_raw_variants": c["raw"],
                    "_hostname": c["hostname"],
                }
            )
        drafts[ntype] = {
            "shard": f"{shard_name}-draft",
            "target_type": target_type,
            "label": f"{ntype} — unreviewed draft",
            "terms": terms,
            "clusters": len(clusters),
            "raw_labels": len(entries),
        }
    return drafts


# ---------------------------------------------------------------------------
# Output


_DRAFT_HEADER = """\
# DRAFT — machine-clustered from {raw_labels} raw label(s) into {clusters} \
group(s), unreviewed.
#
# This file is NOT loaded as vocabulary (it lives outside `vocabulary/`).
# Clustering is string-only: exact match after normalisation, plus a fuzzy
# pass that skips anything shaped like a hostname. It cannot tell you whether
# a group names a real internal system, deserves its own term, or is noise —
# that's `skills/seed-vocabulary.md`'s job.
#
# For each group you keep: move it into vocabulary/<shard>.yaml (drop the
# `_raw_variants` / `_hostname` debug fields, they are not part of the shard
# schema), then delete it from this file. Delete this file once it's empty.
"""


def write_draft_files(drafts: dict[str, dict], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for ntype, draft in drafts.items():
        shard_name, _ = _SHARD_FOR_TYPE[ntype]
        path = out_dir / f"{shard_name}.yaml"
        header = _DRAFT_HEADER.format(raw_labels=draft["raw_labels"], clusters=draft["clusters"])
        body = yaml.safe_dump(
            {
                "shard": draft["shard"],
                "target_type": draft["target_type"],
                "label": draft["label"],
                "terms": draft["terms"],
            },
            allow_unicode=True,
            sort_keys=False,
        )
        path.write_text(header + "\n" + body)
        written.append(path)
    return written
