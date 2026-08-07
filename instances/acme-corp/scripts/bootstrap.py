#!/usr/bin/env python3
"""Deterministic cold start: turn data/seed.yaml into an ingest payload.

This instance is a worked example, not a live scan. The seed data is already
clean — one tech token per entry, no multi-value strings to split, no noise
tokens to filter — so this script only needs to translate it into node/edge
shape and hand it to `capmap ingest`, which does the actual vocabulary
grounding.

    python scripts/bootstrap.py --out payload.bootstrap.json
    capmap ingest payload.bootstrap.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
INSTANCE = HERE.parent
sys.path.insert(0, str(INSTANCE.parent.parent))

from epistemic_agent.merge.canonical import (  # noqa: E402
    canonical_domain_id,
    canonical_repo_id,
    canonical_system_id,
    canonical_team_id,
    canonical_tech_id,
)


def load_seed() -> list[dict]:
    seed_path = INSTANCE / "data" / "seed.yaml"
    return yaml.safe_load(seed_path.read_text())["repos"]


def build(repos: list[dict]) -> tuple[dict, dict]:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    stats: Counter = Counter()

    def put(node_id: str, node_type: str, **props) -> str:
        existing = nodes.get(node_id)
        if existing is None:
            nodes[node_id] = {
                "id": node_id,
                "type": node_type,
                **{k: v for k, v in props.items() if v not in (None, "", [])},
            }
            stats[f"node:{node_type}"] += 1
        else:
            for k, v in props.items():
                if v not in (None, "", []) and not existing.get(k):
                    existing[k] = v
        return node_id

    def link(etype: str, src: str, tgt: str, **props) -> None:
        edges.append({"type": etype, "source": src, "target": tgt, **props})
        stats[f"edge:{etype}"] += 1

    for repo in repos:
        path = repo["path"]
        rid = canonical_repo_id(path)
        put(
            rid,
            "Repo",
            name=repo.get("name") or path.rsplit("/", 1)[-1],
            path=path,
            url=f"https://git.acme.example/{path}",
            description=repo.get("description", ""),
            value=repo.get("value", ""),
            last_commit=repo.get("last_commit", ""),
            visibility="internal",
        )

        team_id = canonical_team_id(repo["team"])
        put(team_id, "Team", name=repo["team"], namespace=repo["team"], kind="group")
        link("OWNED_BY", rid, team_id)

        domain_id = canonical_domain_id(repo["domain"])
        put(domain_id, "Domain", label=repo["domain"])
        link("IN_DOMAIN", rid, domain_id)

        for tech in repo.get("tech", []):
            tid = canonical_tech_id(tech)
            put(tid, "TechStack", label=tech, raw_label=tech)
            link("USES_TECH", rid, tid)

        for system in repo.get("internal_systems", []):
            sid = canonical_system_id(system)
            put(sid, "InternalSystem", label=system.capitalize(), kind="platform")
            link("DEPENDS_ON_INTERNAL", rid, sid, evidence=f"seed:{system}")

    report = {
        "repos": len(repos),
        "nodes_by_type": {
            k.split(":", 1)[1]: v for k, v in sorted(stats.items()) if k.startswith("node:")
        },
        "edges_by_type": {
            k.split(":", 1)[1]: v for k, v in sorted(stats.items()) if k.startswith("edge:")
        },
    }

    payload = {
        "source_files": ["data/seed.yaml"],
        "nodes": list(nodes.values()),
        "edges": edges,
        "changelog_entry": (
            f"Deterministic bootstrap from data/seed.yaml: "
            f"{report['repos']} repos, "
            f"{report['nodes_by_type'].get('Team', 0)} teams, "
            f"{report['nodes_by_type'].get('Domain', 0)} domains, "
            f"{report['nodes_by_type'].get('TechStack', 0)} tech nodes, "
            f"{report['nodes_by_type'].get('InternalSystem', 0)} internal systems. "
            f"Capability and Pattern intentionally not produced — they need the LLM path."
        ),
    }
    return payload, report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="payload.bootstrap.json")
    args = ap.parse_args()

    repos = load_seed()
    payload, report = build(repos)

    out = Path(args.out)
    if not out.is_absolute():
        out = INSTANCE / out
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nPayload written to {out}")
    print(f"  {len(payload['nodes'])} nodes, {len(payload['edges'])} edges")


if __name__ == "__main__":
    main()
