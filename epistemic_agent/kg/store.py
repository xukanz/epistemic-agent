"""Plain-JSON graph store.

The whole knowledge graph is one JSON file, read and mutated through a small
in-memory index. No database, no ORM, no schema-specific query language — the
graph is small enough (thousands, not millions, of nodes) that this is the
simplest thing that could work, and it buys a property nothing else does:
the file can be committed to git and reviewed as a diff.

The on-disk shape::

    {
      "nodes": [{"id": ..., "type": ..., "properties": {...}}],
      "edges": [{"id": ..., "type": ..., "source": ..., "target": ...,
                 "properties": {...}}]
    }

Schema validation is advisory ("warn"): unknown types are recorded and
reported, never rejected. The schema follows the data.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml

EDGE_ID_SEP = "__"


def edge_id(source: str, edge_type: str, target: str) -> str:
    """Canonical, content-derived edge ID. Re-deriving it makes merges idempotent."""
    return f"{source}{EDGE_ID_SEP}{edge_type}{EDGE_ID_SEP}{target}"


class GraphStore:
    """An in-memory graph backed by a single JSON file."""

    def __init__(self, schema: dict | None = None, validation_mode: str = "warn"):
        self.nodes: dict[str, dict] = {}
        self.edges: dict[str, dict] = {}
        self.schema = schema or {}
        self.validation_mode = validation_mode
        self.warnings: list[str] = []

    # ------------------------------------------------------------------
    # Construction

    @classmethod
    def load(cls, path: Path, schema_path: Path | None = None) -> "GraphStore":
        schema = None
        if schema_path and Path(schema_path).exists():
            schema = yaml.safe_load(Path(schema_path).read_text()) or {}
        store = cls(schema=schema)
        if Path(path).exists():
            data = json.loads(Path(path).read_text())
            for n in data.get("nodes", []):
                store.nodes[n["id"]] = n
            for e in data.get("edges", []):
                store.edges[e["id"]] = e
        return store

    @classmethod
    def from_dict(cls, data: dict, schema: dict | None = None) -> "GraphStore":
        store = cls(schema=schema)
        for n in data.get("nodes", []):
            store.nodes[n["id"]] = n
        for e in data.get("edges", []):
            store.edges[e["id"]] = e
        return store

    def to_dict(self) -> dict:
        return {
            "nodes": list(self.nodes.values()),
            "edges": list(self.edges.values()),
        }

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.to_dict()
        # Stable ordering keeps diffs reviewable — this file lives in git.
        payload["nodes"].sort(key=lambda n: (n["type"], n["id"]))
        payload["edges"].sort(key=lambda e: (e["type"], e["source"], e["target"]))
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    # Schema (advisory)

    def _known_node_types(self) -> set[str]:
        return set((self.schema.get("nodes") or {}).keys())

    def _known_edge_types(self) -> set[str]:
        return set((self.schema.get("edges") or {}).keys())

    def _warn_unknown_node(self, node_type: str) -> None:
        known = self._known_node_types()
        if known and node_type not in known:
            msg = f"unknown node type {node_type!r} (not in schema)"
            if msg not in self.warnings:
                self.warnings.append(msg)

    def _warn_unknown_edge(self, edge_type: str) -> None:
        known = self._known_edge_types()
        if known and edge_type not in known:
            msg = f"unknown edge type {edge_type!r} (not in schema)"
            if msg not in self.warnings:
                self.warnings.append(msg)

    # ------------------------------------------------------------------
    # Mutation

    def add_node(self, node_id: str, node_type: str, **properties: Any) -> dict:
        """Add or merge a node. Existing non-empty properties are not clobbered
        by empty incoming values, but real values do overwrite."""
        self._warn_unknown_node(node_type)
        existing = self.nodes.get(node_id)
        if existing is None:
            node = {"id": node_id, "type": node_type, "properties": dict(properties)}
            self.nodes[node_id] = node
            return node
        props = existing.setdefault("properties", {})
        for k, v in properties.items():
            if v in (None, "", [], {}):
                continue
            props[k] = v
        return existing

    def add_edge(
        self,
        eid: str | None,
        edge_type: str,
        source: str,
        target: str,
        **properties: Any,
    ) -> dict:
        self._warn_unknown_edge(edge_type)
        eid = eid or edge_id(source, edge_type, target)
        existing = self.edges.get(eid)
        if existing is None:
            edge = {
                "id": eid,
                "type": edge_type,
                "source": source,
                "target": target,
                "properties": dict(properties),
            }
            self.edges[eid] = edge
            return edge
        props = existing.setdefault("properties", {})
        for k, v in properties.items():
            if v in (None, "", [], {}):
                continue
            props[k] = v
        return existing

    def remove_node(self, node_id: str, cascade: bool = True) -> None:
        self.nodes.pop(node_id, None)
        if cascade:
            for eid in [
                eid
                for eid, e in self.edges.items()
                if e["source"] == node_id or e["target"] == node_id
            ]:
                del self.edges[eid]

    def set_properties(self, node_id: str, props: dict) -> bool:
        node = self.nodes.get(node_id)
        if node is None:
            return False
        node.setdefault("properties", {}).update(props)
        return True

    # ------------------------------------------------------------------
    # Query

    def get_node(self, node_id: str) -> dict | None:
        return self.nodes.get(node_id)

    def query_nodes(self, type: str | None = None, **prop_filters: Any) -> list[dict]:
        out = []
        for n in self.nodes.values():
            if type is not None and n["type"] != type:
                continue
            props = n.get("properties") or {}
            if any(props.get(k) != v for k, v in prop_filters.items()):
                continue
            out.append(n)
        return out

    def query_edges(self, type: str | None = None, source: str | None = None,
                    target: str | None = None) -> list[dict]:
        out = []
        for e in self.edges.values():
            if type is not None and e["type"] != type:
                continue
            if source is not None and e["source"] != source:
                continue
            if target is not None and e["target"] != target:
                continue
            out.append(e)
        return out

    def get_neighbors(
        self,
        node_id: str,
        edge_type: str | None = None,
        direction: str = "both",
    ) -> list[str]:
        out: list[str] = []
        for e in self.edges.values():
            if edge_type is not None and e["type"] != edge_type:
                continue
            if direction in ("out", "both") and e["source"] == node_id:
                out.append(e["target"])
            if direction in ("in", "both") and e["target"] == node_id:
                out.append(e["source"])
        return out

    def adjacency(self, edge_type: str) -> dict[str, set[str]]:
        """source -> {targets} for one edge type. Cheap to build, used a lot by
        the analysis views, so build once and pass it around."""
        adj: dict[str, set[str]] = defaultdict(set)
        for e in self.edges.values():
            if e["type"] == edge_type:
                adj[e["source"]].add(e["target"])
        return adj

    def reverse_adjacency(self, edge_type: str) -> dict[str, set[str]]:
        adj: dict[str, set[str]] = defaultdict(set)
        for e in self.edges.values():
            if e["type"] == edge_type:
                adj[e["target"]].add(e["source"])
        return adj

    # ------------------------------------------------------------------
    # Stats

    def get_statistics(self) -> dict:
        node_types: dict[str, int] = defaultdict(int)
        for n in self.nodes.values():
            node_types[n["type"]] += 1
        edge_types: dict[str, int] = defaultdict(int)
        for e in self.edges.values():
            edge_types[e["type"]] += 1
        connected: set[str] = set()
        for e in self.edges.values():
            connected.add(e["source"])
            connected.add(e["target"])
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges),
            "node_types": dict(sorted(node_types.items())),
            "edge_types": dict(sorted(edge_types.items())),
            "connectivity": {
                "connected_nodes": len(connected & set(self.nodes)),
                "isolated_nodes": len(set(self.nodes) - connected),
            },
            "schema_warnings": list(self.warnings),
        }


def iter_nodes(kg: dict, node_type: str | None = None) -> Iterable[dict]:
    """Iterate the plain dict form — used by modules that take a raw KG dict."""
    for n in kg.get("nodes", []):
        if node_type is None or n["type"] == node_type:
            yield n


def prop(node: dict, key: str, default: str = "") -> Any:
    return (node.get("properties") or {}).get(key, default) or default
