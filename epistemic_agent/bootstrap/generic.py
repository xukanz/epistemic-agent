"""Config-driven deterministic cold start for flat, one-record-per-repo sources.

`instances/acme-corp/scripts/bootstrap.py` showed that when the source data is
already a flat list of repo records, the code that turns it into an ingest
payload is pure field shuffling plus the canonical-ID helpers in
`merge/canonical.py` — nothing organisation-specific. This module lifts that
shape into a reusable function driven by a `bootstrap:` block in
`config/project.yaml`, so a new instance whose inventory already looks like
"one row per repo" needs zero Python: only a field-mapping config and a data
file.

Sources that need joining several artefacts, parsing markdown tables, or
resolving hostnames against the vocabulary at bootstrap time (like
`instances/acme-gitlab-agents/scripts/bootstrap.py`) still need a hand-written
script — see `skills/bootstrap-instance.md` for that path.
"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from epistemic_agent.merge.canonical import (
    canonical_domain_id,
    canonical_repo_id,
    canonical_system_id,
    canonical_team_id,
    canonical_tech_id,
    is_noise,
    normalise_tech_label,
    split_multi_value,
    team_namespace,
)
from epistemic_agent.onto.client import resolve_backend

# Generic field name -> key expected in each record. `None` means "derive it
# instead of reading a field" (team, url). Overridable per-instance via
# `bootstrap.fields` in project.yaml — only the keys that differ need stating.
DEFAULT_FIELDS: dict[str, str | None] = {
    "path": "path",
    "name": "name",
    "team": None,
    "domain": "domain",
    "tech": "tech",
    "internal_systems": "internal_systems",
    "description": "description",
    "value": "value",
    "last_commit": "last_commit",
    "url": None,
}

# Comma variants seen in the wild, split before the finer-grained
# `split_multi_value` (which handles `/`, `|`, `、`, lone `+`).
_COMMA_RE = re.compile(r"[,，]")


def _split_comma(raw: str) -> list[str]:
    return [p.strip() for p in _COMMA_RE.split(raw) if p.strip()]


def _tokens(value: Any) -> list[str]:
    """Normalise a `tech` / `internal_systems` field into a flat token list.

    Accepts a list (already one token per entry — still run each through
    `split_multi_value` in case an entry itself carries a `/`) or a raw string
    (comma-separated first, then `split_multi_value` per piece).
    """
    if value is None:
        return []
    if isinstance(value, list):
        items = [str(v) for v in value]
    else:
        items = _split_comma(str(value))
    out: list[str] = []
    for item in items:
        out.extend(split_multi_value(item))
    return out


def _get(record: dict, field_name: str | None) -> Any:
    if field_name is None:
        return None
    return record.get(field_name)


def _load_records(path: Path, fmt: str, list_key: str) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"bootstrap.file does not exist: {path}")
    if fmt == "csv":
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) if fmt == "yaml" else json.loads(text)
    if isinstance(data, list):
        return data
    if not isinstance(data, dict) or list_key not in data:
        raise SystemExit(
            f"bootstrap.list_key {list_key!r} not found in {path} "
            f"(top-level keys: {list(data) if isinstance(data, dict) else type(data)})"
        )
    return data[list_key]


def run_generic_bootstrap(config: dict, instance_root: Path) -> tuple[dict, dict]:
    """Turn the flat source described by `config["bootstrap"]` into an ingest
    payload. Returns (payload, report) — same shape as the hand-written
    `bootstrap.py` scripts, so the CLI wrapper and `docs/new-instance.md`'s
    "how to read the output" section apply unchanged.
    """
    bcfg = config.get("bootstrap") or {}
    fmt = bcfg.get("format", "yaml")
    file_rel = bcfg.get("file", "data/seed.yaml")
    list_key = bcfg.get("list_key", "repos")
    fields = {**DEFAULT_FIELDS, **(bcfg.get("fields") or {})}
    noise_filter = bcfg.get("noise_filter", True)
    internal_shard = bcfg.get("internal_shard", "internal-system")

    source_path = instance_root / file_rel
    records = _load_records(source_path, fmt, list_key)

    # Vocabulary-first, same reasoning as acme-gitlab-agents: an empty
    # `vocabulary/` (the fresh-instance default) just means every lookup below
    # falls through to the framework's 12 generic shards, which is fine — this
    # only *helps* classify things correctly when the instance vocabulary
    # exists, it never blocks running with none.
    vocab = resolve_backend(config, instance_root)
    exact_tech = vocab.alias_map("TechStack")
    term_shard = {t["term_id"]: t["shard"] for t in vocab._terms}  # noqa: SLF001
    term_label = {t["term_id"]: t["label"] for t in vocab._terms}  # noqa: SLF001

    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    stats: Counter = Counter()
    repos_without_domain = 0
    repos_without_team_field = 0

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

    for rec in records:
        path = _get(rec, fields["path"])
        if not path:
            continue
        rid = canonical_repo_id(path)
        put(
            rid,
            "Repo",
            name=_get(rec, fields["name"]) or path.rsplit("/", 1)[-1],
            path=path,
            url=_get(rec, fields["url"]),
            description=_get(rec, fields["description"]) or "",
            value=_get(rec, fields["value"]) or "",
            last_commit=_get(rec, fields["last_commit"]) or "",
            visibility="internal",
        )

        team_field = fields["team"]
        if team_field is not None and _get(rec, team_field):
            # An explicit team field is a deliberate assignment the caller
            # made, not a guess from path shape — safe to call it a group.
            team_val = _get(rec, team_field)
            team_kind = "group"
        else:
            team_val = team_namespace(path)
            team_kind = "unknown"
            repos_without_team_field += 1
        team_id = canonical_team_id(team_val)
        put(team_id, "Team", name=team_val, namespace=team_val, kind=team_kind)
        link("OWNED_BY", rid, team_id)

        domain_val = _get(rec, fields["domain"])
        if domain_val:
            domain_id = canonical_domain_id(domain_val)
            put(domain_id, "Domain", label=domain_val)
            link("IN_DOMAIN", rid, domain_id)
        else:
            repos_without_domain += 1

        for token in _tokens(_get(rec, fields["tech"])):
            norm = normalise_tech_label(token)
            if not norm:
                continue

            # A dependency token can itself name an internal system (see
            # acme-gitlab-agents). Model it as InternalSystem, not TechStack,
            # so the two views don't each count it once.
            itid = exact_tech.get(norm)
            if itid and term_shard.get(itid) == internal_shard:
                sid = "sys-" + normalise_tech_label(term_label[itid]).replace(" ", "-")
                put(sid, "InternalSystem", label=term_label[itid], kind="dependency")
                link("DEPENDS_ON_INTERNAL", rid, sid, evidence=f"tech:{token}")
                continue

            known = norm in exact_tech
            if not known and noise_filter and is_noise(token):
                tid = canonical_tech_id(token)
                put(
                    f"untyped-{tid.removeprefix('tech-')}",
                    "Untyped",
                    label=token.strip(),
                    suggested_type="TechStack",
                    _note="tech token that looks like noise",
                )
                stats["noise_tokens"] += 1
                continue

            tid = canonical_tech_id(token)
            put(tid, "TechStack", label=token.strip(), raw_label=token)
            link("USES_TECH", rid, tid)

        for token in _tokens(_get(rec, fields["internal_systems"])):
            sid = canonical_system_id(token)
            put(sid, "InternalSystem", label=token.strip().capitalize(), kind="platform")
            link("DEPENDS_ON_INTERNAL", rid, sid, evidence=f"seed:{token}")

    report = {
        "repos": len(records),
        "repos_without_domain": repos_without_domain,
        "repos_without_team_field": repos_without_team_field,
        "noise_tokens": stats["noise_tokens"],
        "nodes_by_type": {
            k.split(":", 1)[1]: v for k, v in sorted(stats.items()) if k.startswith("node:")
        },
        "edges_by_type": {
            k.split(":", 1)[1]: v for k, v in sorted(stats.items()) if k.startswith("edge:")
        },
    }

    payload = {
        "source_files": [str(file_rel)],
        "nodes": list(nodes.values()),
        "edges": edges,
        "changelog_entry": (
            f"Deterministic bootstrap from {file_rel} ({fmt}): "
            f"{report['repos']} repos, "
            f"{report['nodes_by_type'].get('Team', 0)} teams, "
            f"{report['nodes_by_type'].get('Domain', 0)} domains, "
            f"{report['nodes_by_type'].get('TechStack', 0)} tech nodes, "
            f"{report['nodes_by_type'].get('InternalSystem', 0)} internal systems. "
            f"Capability and Pattern intentionally not produced — they need the LLM path."
        ),
    }
    return payload, report
