"""The single write entry point into the knowledge graph.

The agent never writes to the graph file directly — it builds a JSON payload
and hands it to `ingest_entities`. Grounding happens here too: the payload
carries raw labels, and `ground_payload` applies the 0.70 / 0.50 routing bands
in code before anything is written. Keeping that logic in code rather than in
a prompt means the policy is implemented once, not re-derived by whoever wrote
the calling prompt.

The payload format::

    {
      "source_files": ["data/raw/..."],
      "nodes": [{"id": ..., "type": ..., "label": ...}],
      "edges": [{"type": ..., "source": ..., "target": ...}],
      "updates": [{"id": ..., "action": "set"|"remove"|"deprecate", "set": {...}}],
      "changelog_entry": "..."
    }

Four invariants make the graph trustworthy: every node gets `_sources`, every
run appends to the changelog, removal is a soft flag rather than a delete, and
processed files are recorded with a content hash so re-running is a no-op.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

from epistemic_agent.kg.store import GraphStore, edge_id

# Node types that carry meaning and therefore need grounding. Structural types
# (Repo, Team, Person, Domain) are identified by natural keys and never
# grounded — grounding them would be a category error.
DEFAULT_GROUNDABLE = {
    "TechStack": "TechStack",
    "Capability": "TechStack",
    "Pattern": "Pattern",
    "InternalSystem": "TechStack",
}


def _load_manifest(manifest_path: Path) -> dict:
    if manifest_path.exists():
        return json.loads(manifest_path.read_text())
    return {"files": []}


def _save_manifest(manifest: dict, manifest_path: Path) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")


def _file_hash(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _append_changelog(entry: str, changelog_path: Path, source_files: list[str] | None) -> None:
    changelog_path.parent.mkdir(parents=True, exist_ok=True)
    date = datetime.now().strftime("%Y-%m-%d %H:%M")
    sources = ", ".join(source_files) if source_files else "agent action"
    with changelog_path.open("a") as f:
        f.write(f"\n## {date}\n\n**Source**: {sources}\n\n{entry}\n")


# ---------------------------------------------------------------------------
# Grounding


def ground_payload(
    payload: dict,
    vocabulary,
    queue=None,
    project_name: str = "",
    groundable: dict[str, str] | None = None,
) -> dict:
    """Resolve node labels against the vocabulary and apply the routing bands.

    * score ≥ 0.70 → set `term_id` / `ontology_id` / `_grounding_score`
    * 0.50 ≤ score < 0.70 → set `_grounding_candidates`, emit a review item
    * score < 0.50 → leave ungrounded, record the label as a vocabulary
      suggestion so the gap is visible rather than lost

    Returns counts. Mutates `payload["nodes"]` in place.
    """
    from epistemic_agent.onto.client import (
        AUTO_GROUND_THRESHOLD,
        CANDIDATE_THRESHOLD,
    )

    groundable = groundable or DEFAULT_GROUNDABLE
    stats = {"grounded": 0, "candidate": 0, "ungrounded": 0, "skipped": 0, "requeued": 0}
    suggestions: list[dict] = []
    # Read the queue once for the whole batch, not once per node.
    known_keys = queue.pending_keys() if queue is not None else set()

    for node in payload.get("nodes", []):
        target_type = groundable.get(node.get("type", ""))
        if target_type is None:
            stats["skipped"] += 1
            continue
        # `label` first, `raw_label` only as a fallback. `raw_label` preserves
        # the source string *before* multi-value splitting — for a token like
        # "OpenAI/Anthropic" both resulting nodes carry it — so grounding on it
        # resolves the OpenAI node against Anthropic. It is provenance, not
        # input.
        label = node.get("label") or node.get("name") or node.get("raw_label") or ""
        if not label:
            stats["skipped"] += 1
            continue

        result = vocabulary.ground(label, target_type=target_type)
        best = result.best

        if best is not None and best.score >= AUTO_GROUND_THRESHOLD:
            node["term_id"] = best.term_id
            node["ontology_id"] = best.shard
            node["_grounding_score"] = round(best.score, 3)
            if best.parent:
                node["_parent_term"] = best.parent
            # Grounding is where a node acquires its vocabulary identity, so
            # this is where the authoritative name is set. Doing it only at
            # merge time meant the next ingest overwrote it again, and the
            # graph flipped between `Portkey` and `portkey-ai` per run. The
            # source string stays in `raw_label`.
            node.setdefault("raw_label", label)
            node["label"] = best.label
            stats["grounded"] += 1
        elif best is not None and best.score >= CANDIDATE_THRESHOLD:
            node["_grounding_score"] = round(best.score, 3)
            node["_grounding_candidates"] = [c.as_dict() for c in result.candidates[:3]]
            stats["candidate"] += 1
            if queue is not None:
                from epistemic_agent.review.emitters import emit_grounding_candidate

                written = queue.append_unique(
                    emit_grounding_candidate(
                        source_project=project_name,
                        entity_label=label,
                        entity_id=node["id"],
                        candidates=[c.as_dict() for c in result.candidates[:3]],
                        cluster_score=best.score,
                    ),
                    known=known_keys,
                )
                if not written:
                    stats["requeued"] += 1
        else:
            stats["ungrounded"] += 1
            suggestions.append({"label": label, "node_id": node["id"], "type": node["type"]})

    payload.setdefault("_grounding_stats", {}).update(stats)
    payload["_vocabulary_suggestions"] = suggestions
    return stats


def write_vocabulary_suggestions(suggestions: list[dict], path: Path) -> None:
    """Append ungrounded labels to a review file.

    An ungrounded label is either a real vocabulary gap or extraction noise,
    and both need a human to look at it once — so nothing here gets silently
    dropped just because it did not score high enough to auto-ground.
    """
    if not suggestions:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else "# Vocabulary suggestions\n"
    seen = set()
    for line in existing.splitlines():
        if line.startswith("- `"):
            seen.add(line.split("`")[1])
    new = [s for s in suggestions if s["label"] not in seen]
    if not new:
        return
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [existing.rstrip(), "", f"## {stamp}", ""]
    for s in sorted(new, key=lambda d: d["label"]):
        lines.append(f"- `{s['label']}` — {s['type']} ({s['node_id']})")
    path.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Ingest


def build_redirects(store: GraphStore) -> dict[str, str]:
    """Map already-merged IDs to their canonical node.

    Without this, any payload written before a merge re-creates every node the
    merge folded away — silently undoing it. `apply_merges` records the
    provenance (`_renamed_from`, `_merged_from`) precisely so that the merge
    survives the next ingest.
    """
    redirects: dict[str, str] = {}
    for node in store.nodes.values():
        props = node.get("properties") or {}
        old = props.get("_renamed_from")
        if old and old != node["id"]:
            redirects[old] = node["id"]
        for merged in props.get("_merged_from") or []:
            if merged != node["id"]:
                redirects[merged] = node["id"]
    # Never redirect onto something that was itself merged away.
    return {k: v for k, v in redirects.items() if k not in store.nodes}


def ingest_entities(
    entities_data: dict,
    kg_path: Path,
    schema_path: Path,
    manifest_path: Path,
    changelog_path: Path,
) -> tuple[list[str], dict]:
    """Process additions, updates, and removals. Returns (node_ids, stats)."""
    store = GraphStore.load(kg_path, schema_path)
    redirects = build_redirects(store)

    stats = {
        "nodes_added": 0,
        "nodes_updated": 0,
        "edges_added": 0,
        "updated": 0,
        "removed": 0,
        "deprecated": 0,
    }
    node_ids: list[str] = []
    today = datetime.now().strftime("%Y-%m-%d")
    source_files = entities_data.get("source_files", [])
    source_str = ", ".join(source_files)

    for raw in entities_data.get("nodes", []):
        node = dict(raw)
        raw_id = node.pop("id")
        node_id = redirects.get(raw_id, raw_id)
        was_redirected = node_id != raw_id
        if was_redirected:
            stats["redirected"] = stats.get("redirected", 0) + 1
            # A redirected node is an alias of the survivor, not an update to
            # it. Letting `portkey-ai` overwrite the canonical node's label
            # would undo what the merge just decided.
            for identity_key in ("label", "name", "raw_label"):
                node.pop(identity_key, None)
        node_type = node.pop("type")
        existed = node_id in store.nodes
        node.setdefault("_last_updated", today)
        if source_str:
            prior = ""
            if existed:
                prior = (store.nodes[node_id].get("properties") or {}).get("_sources", "")
            merged = "; ".join(x for x in [prior, source_str] if x)
            # Keep provenance from growing without bound on repeated ingests.
            parts = []
            for p in merged.split("; "):
                if p and p not in parts:
                    parts.append(p)
            node["_sources"] = "; ".join(parts)
        store.add_node(node_id, node_type, **node)
        node_ids.append(node_id)
        stats["nodes_updated" if existed else "nodes_added"] += 1

    for raw in entities_data.get("edges", []):
        edge = dict(raw)
        edge_type = edge.pop("type")
        src_in, tgt_in = edge.pop("source"), edge.pop("target")
        source = redirects.get(src_in, src_in)
        target = redirects.get(tgt_in, tgt_in)
        # Always re-derive: a caller-supplied ID would be stale after a
        # redirect, and derived IDs are what make re-ingest idempotent.
        edge.pop("id", None)
        eid = edge_id(source, edge_type, target)
        edge.setdefault("_last_updated", today)
        store.add_edge(eid, edge_type, source, target, **edge)
        stats["edges_added"] += 1

    for update in entities_data.get("updates", []):
        entity_id = update["id"]
        action = update.get("action", "set")
        node = store.get_node(entity_id)
        if node is None:
            print(f"  WARN: update target not found: {entity_id}", file=sys.stderr)
            continue
        props = node.setdefault("properties", {})

        if action == "set":
            props.update(update.get("set", {}))
            props["_last_updated"] = today
            stats["updated"] += 1
        elif action == "remove":
            props["_removed"] = True
            props["_removed_date"] = today
            props["_removed_reason"] = update.get("reason", "no reason given")
            stats["removed"] += 1
        elif action == "deprecate":
            props["status"] = "deprecated"
            props["_deprecated_date"] = today
            props["_deprecated_reason"] = update.get("reason", "no reason given")
            props["_last_updated"] = today
            stats["deprecated"] += 1

    store.save(kg_path)
    stats["schema_warnings"] = store.warnings

    if entry := entities_data.get("changelog_entry"):
        _append_changelog(entry, changelog_path, source_files)

    return node_ids, stats


def mark_processed(
    source_file: str, manifest_path: Path, node_ids: list[str] | None = None
) -> None:
    manifest = _load_manifest(manifest_path)
    source_path = Path(source_file)
    entry: dict = {
        "file": str(source_file),
        "date": datetime.now().isoformat(timespec="seconds"),
        "kg_nodes_created": node_ids or [],
    }
    if source_path.exists():
        entry["hash"] = _file_hash(source_path)
    existing = [f for f in manifest["files"] if f["file"] == str(source_file)]
    if existing:
        existing[0].update(entry)
    else:
        manifest["files"].append(entry)
    _save_manifest(manifest, manifest_path)


def unprocessed_files(paths: list[Path], manifest_path: Path) -> list[Path]:
    """Files whose content hash is not already recorded — the idempotence check."""
    manifest = _load_manifest(manifest_path)
    known = {f["file"]: f.get("hash") for f in manifest.get("files", [])}
    out = []
    for p in paths:
        if not p.exists():
            continue
        prior = known.get(str(p))
        if prior is None or prior != _file_hash(p):
            out.append(p)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest entities into a capability-map KG")
    parser.add_argument("input", nargs="?", help="Path to entities JSON file")
    parser.add_argument("--stdin", action="store_true")
    parser.add_argument("--mark-processed", metavar="FILE")
    parser.add_argument("--kg", required=True, metavar="PATH")
    parser.add_argument("--schema", required=True, metavar="PATH")
    parser.add_argument("--manifest", required=True, metavar="PATH")
    parser.add_argument("--changelog", required=True, metavar="PATH")
    args = parser.parse_args()

    kg_path = Path(args.kg)
    manifest_path = Path(args.manifest)

    if args.mark_processed:
        mark_processed(args.mark_processed, manifest_path)
        return

    if args.stdin:
        entities = json.load(sys.stdin)
    elif args.input:
        entities = json.loads(Path(args.input).read_text())
    else:
        parser.print_help()
        sys.exit(1)

    node_ids, stats = ingest_entities(
        entities, kg_path, Path(args.schema), manifest_path, Path(args.changelog)
    )
    for sf in entities.get("source_files", []):
        mark_processed(sf, manifest_path, node_ids)

    parts = [f"{v} {k.replace('_', ' ')}" for k, v in stats.items()
             if isinstance(v, int) and v]
    print(f"  Result: {', '.join(parts) or 'no changes'}")
    for w in stats.get("schema_warnings") or []:
        print(f"  SCHEMA: {w}", file=sys.stderr)


if __name__ == "__main__":
    main()
