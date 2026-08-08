"""Tools for the standalone agent runtime.

Most tools wrap an existing, already-deterministic library function — nothing
here re-implements ingest, merge, or grounding logic. The plain `_snake_case`
functions below are the actual implementations (unit-tested directly);
`build_openai_tools()` wraps them in OpenAI function-calling shape for one
`Project` for a single agent session.

Two guardrail mechanisms coexist here, deliberately different in kind:

1. **Absence.** Writing `vocabulary/*.yaml`, resolving review-queue items,
   and asserting `DUPLICATES` edges are things this framework's own docs and
   code require a human to decide (see `review/emitters.py`: "Never
   auto-assert the DUPLICATES edge; always route through a human"). There is
   no tool function for any of them, full stop — not a permission check, an
   absent capability.
2. **Restriction.** `write_file` is a real, general-shaped write tool — it
   exists because setup files (`config/project.yaml`, `schema/kg-schema.yaml`,
   `scripts/bootstrap.py`) are cheap to get wrong and rewrite, unlike a
   vocabulary decision or a merge. It enforces its own path allowlist in code
   (`_WRITABLE`) rather than relying on the model to self-restrict, and that
   allowlist excludes `vocabulary/`, `kg/`, and `review/` by construction —
   see `test_write_file_refuses_*` in `tests/test_smoke.py` for the
   guarantee this depends on.

`read_file` and `fetch_url` carry no path/domain restriction — reading is the
same trust boundary the human already operates in (their own shell can `cat`
or `curl` anything they can), so there is nothing to gate.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from epistemic_agent.project import Project

_NO_KG = (
    "No knowledge graph exists yet at this instance ({path}). Do NOT scaffold, "
    "bootstrap, or ingest yet — follow the first-run protocol in CLAUDE.md: "
    "have a conversation with the human about what this map is for, the two "
    "hard prerequisites (where the vocabulary comes from, who reviews the "
    "queue), and the schema, before anything gets written."
)


# ---------------------------------------------------------------------------
# Plain implementations


def _orient_state(project: Project) -> str:
    if not project.kg_path.exists():
        return _NO_KG.format(path=project.kg_path)

    from epistemic_agent.health.manifest import generate_manifest
    from epistemic_agent.kg.store import GraphStore
    from epistemic_agent.review.queue import ReviewQueue

    store = GraphStore.load(project.kg_path, project.schema_path)
    stats = store.get_statistics()
    manifest = generate_manifest(
        kg_path=project.kg_path,
        output_path=project.health_path,
        stale_days=365,
        semantic_types=project.semantic_types(),
        vocabulary=project.vocabulary(),
    )
    pending = ReviewQueue(project.review_dir).pending_count()

    lines = [
        f"{project.name}: {stats['total_nodes']} nodes, {stats['total_edges']} edges",
        "Nodes by type: " + ", ".join(f"{k}={v}" for k, v in stats["node_types"].items()),
        f"Connectivity: {stats['connectivity']['connected_nodes']} connected, "
        f"{stats['connectivity']['isolated_nodes']} isolated",
        "",
        "Health signals (outputs, not problems to silently fix):",
        f"  ungrounded semantic nodes: {sum(len(v) for v in manifest.ungrounded_nodes.values())}",
        f"  grounding candidates (0.50-0.70, need a human): {len(manifest.grounding_candidates)}",
        f"  fuzzy duplicate pairs: {len(manifest.fuzzy_duplicate_pairs)}",
        f"  schema gap clusters: {len(manifest.schema_gap_clusters)}",
        f"  orphan nodes: {len(manifest.orphan_nodes)}",
        f"  stale nodes: {len(manifest.stale_nodes)}",
        f"  bus-factor-1 capabilities: {len(manifest.bus_factor_one)}",
        f"  duplicate-effort candidates: {len(manifest.duplicate_clusters)}",
        f"  uncovered vocabulary terms: {len(manifest.uncovered_vocabulary)}",
        "",
        f"Pending review items: {pending} (only a human resolves these, via `capmap review`)",
    ]
    for w in stats["schema_warnings"]:
        lines.append(f"SCHEMA WARNING: {w}")
    return "\n".join(lines)


def _read_skill(project: Project, name: str) -> str:
    skills_dir = project.root / "skills"
    path = skills_dir / f"{name}.md"
    if not path.exists():
        available = sorted(p.stem for p in skills_dir.glob("*.md")) if skills_dir.exists() else []
        return f"No skill named {name!r}. Available: {', '.join(available) or '(none found)'}"
    return path.read_text()


def _run_bootstrap(project: Project, out: str = "payload.bootstrap.json") -> str:
    script = project.root / "scripts" / "bootstrap.py"
    if not script.exists():
        return (
            f"No scripts/bootstrap.py at {script}. Follow skills/bootstrap-instance.md "
            "to create one — try the generic config-driven path first."
        )
    proc = subprocess.run(
        [sys.executable, str(script), "--out", out],
        cwd=project.root,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return f"bootstrap.py failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout


def _ingest_payload(project: Project, path: str, force: bool = False) -> str:
    from epistemic_agent.ingest.document import (
        ground_payload,
        ingest_entities,
        mark_processed,
        unprocessed_files,
        write_vocabulary_suggestions,
    )
    from epistemic_agent.review.queue import ReviewQueue

    payload_path = Path(path)
    if not payload_path.exists():
        return f"No payload file at {payload_path} (resolved from the current working directory)."
    data = json.loads(payload_path.read_text())

    declared = [Path(p) for p in data.get("source_files", [])]
    if declared and not force:
        pending = unprocessed_files(declared, project.manifest_path)
        if not pending:
            return (
                f"Nothing to do — all {len(declared)} source files are unchanged since "
                "the last ingest. Call again with force=True to ingest anyway."
            )

    q = ReviewQueue(project.review_dir)
    gstats = ground_payload(data, project.vocabulary(), queue=q, project_name=project.name)
    write_vocabulary_suggestions(data.get("_vocabulary_suggestions", []), project.suggestions_path)

    node_ids, stats = ingest_entities(
        data, project.kg_path, project.schema_path, project.manifest_path, project.changelog_path
    )
    for sf in data.get("source_files", []):
        mark_processed(sf, project.manifest_path, node_ids)

    lines = [
        f"Grounding: {gstats['grounded']} grounded, {gstats['candidate']} candidates, "
        f"{gstats['ungrounded']} ungrounded, {gstats['skipped']} structural",
        f"Ingest: +{stats['nodes_added']} nodes, ~{stats['nodes_updated']} updated, "
        f"+{stats['edges_added']} edges",
    ]
    if stats.get("redirected"):
        lines.append(f"{stats['redirected']} redirected to post-merge IDs")
    for w in stats.get("schema_warnings") or []:
        lines.append(f"SCHEMA WARNING: {w}")
    return "\n".join(lines)


def _vocab_draft(
    project: Project,
    payload: str | None = None,
    from_suggestions: bool = False,
    fuzzy_threshold: float = 0.90,
) -> str:
    from epistemic_agent.onto.draft import (
        build_vocab_draft,
        labels_from_payload,
        labels_from_suggestions_md,
        write_draft_files,
    )

    if from_suggestions:
        if not project.suggestions_path.exists():
            return f"No suggestions file at {project.suggestions_path}."
        labels = labels_from_suggestions_md(project.suggestions_path.read_text())
    else:
        if not payload:
            return "Pass a payload path, or set from_suggestions=True."
        payload_path = Path(payload)
        if not payload_path.exists():
            return f"No payload file at {payload_path}."
        labels = labels_from_payload(json.loads(payload_path.read_text()))

    if not labels:
        return "Nothing to cluster."

    drafts = build_vocab_draft(labels, fuzzy_threshold=fuzzy_threshold)
    out_dir = project.root / "kg" / "vocabulary-draft"
    written = write_draft_files(drafts, out_dir)

    lines = [f"{ntype}: {d['raw_labels']} labels -> {d['clusters']} groups" for ntype, d in drafts.items()]
    lines += [f"Wrote {p}" for p in written]
    lines.append(
        "Draft only — not loaded as vocabulary and never will be automatically. Review with "
        "skills/seed-vocabulary.md, then tell the human exactly what to add to vocabulary/*.yaml; "
        "you cannot write that file yourself."
    )
    return "\n".join(lines)


def _format_merge_result(result: dict) -> str:
    if "error" in result:
        return result["error"]
    prefix = "DRY RUN — " if result["dry_run"] else ""
    lines = [
        f"{prefix}Merges: {result['merges']} over {result['rounds']} round(s)",
        f"Nodes: {result['before_nodes']} -> {result['after_nodes']}",
        f"Edges: {result['before_edges']} -> {result['after_edges']}",
        f"Invariant violations: {result['violations']}",
        f"Report: {result['report_path']}",
    ]
    if result["hit_max_rounds"]:
        lines.append("WARNING: hit the round cap — the merge has not converged, check the report.")
    return "\n".join(lines)


def _merge_dry_run(project: Project) -> str:
    if not project.kg_path.exists():
        return _NO_KG.format(path=project.kg_path)
    from epistemic_agent.merge.strategies import run_merge

    return _format_merge_result(run_merge(project, dry_run=True))


def _merge_apply(project: Project) -> str:
    if not project.kg_path.exists():
        return _NO_KG.format(path=project.kg_path)
    from epistemic_agent.merge.strategies import run_merge

    return _format_merge_result(run_merge(project, dry_run=False))


def _view(project: Project, name: str, query: str = "", by: str = "shard", limit: int = 20) -> str:
    if not project.kg_path.exists():
        return _NO_KG.format(path=project.kg_path)
    from epistemic_agent.analysis import views as V

    idx = V.GraphIndex(project.load_kg())
    if name == "coverage":
        rows = V.view_coverage(idx, by=by)
    elif name == "duplicates":
        rows = V.view_duplicates(idx)
    elif name == "gaps":
        rows = V.view_gaps(idx, project.vocabulary())
    elif name == "experts":
        if not query:
            return "The experts view needs a query, e.g. a technology name."
        rows = V.view_experts(idx, query, project.vocabulary(), limit=limit or 10_000)
    elif name == "risk":
        rows = V.view_risk(idx)
    else:
        return f"Unknown view {name!r} — coverage | duplicates | gaps | experts | risk"
    return json.dumps(rows, ensure_ascii=False, indent=2)[:8000]


def _review_status(project: Project) -> str:
    from collections import Counter

    from epistemic_agent.review.queue import ReviewQueue

    items = ReviewQueue(project.review_dir).list_items()
    if not items:
        return "No pending review items."
    by_type = Counter(i.source_type for i in items)
    lines = [f"{len(items)} pending review items", *(f"  {k}: {v}" for k, v in by_type.most_common())]
    lines.append("")
    lines.append(
        "Read-only — resolving these needs a human running `capmap review --reviewer <name>`."
    )
    return "\n".join(lines)


_MAX_READ_CHARS = 20_000


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n...[truncated, {len(text)} chars total]"


def _read_file(path: str, max_chars: int = _MAX_READ_CHARS) -> str:
    """No path restriction — same trust boundary as the human's own shell."""
    p = Path(path)
    if not p.exists():
        return f"No file at {p}."
    if p.is_dir():
        return f"{p} is a directory: " + ", ".join(sorted(x.name for x in p.iterdir())[:200])
    return _truncate(p.read_text(errors="replace"), max_chars)


def _fetch_url(url: str, max_chars: int = _MAX_READ_CHARS) -> str:
    """stdlib-only GET — deliberately no new dependency just for this."""
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310 - user-directed fetch
            charset = resp.headers.get_content_charset() or "utf-8"
            text = resp.read().decode(charset, errors="replace")
    except Exception as exc:  # noqa: BLE001 - surfaced to the model as a tool result, not raised
        return f"Fetch failed: {exc}"
    return _truncate(text, max_chars)


# Everything else under the instance root is off-limits by construction — in
# particular vocabulary/ (human-only term decisions), kg/ (must go through
# ingest, never a direct write), and review/ (human-only queue resolution).
_WRITABLE_FILES = {"config/project.yaml", "schema/kg-schema.yaml", "scripts/bootstrap.py"}
_WRITABLE_DIRS = ("data/raw/",)


def _write_file(project: Project, path: str, content: str) -> str:
    rel = os.path.normpath(path).replace(os.sep, "/")
    if rel.startswith("..") or os.path.isabs(path):
        return f"Refused: {path!r} is outside the instance directory."
    if rel not in _WRITABLE_FILES and not any(rel.startswith(d) for d in _WRITABLE_DIRS):
        return (
            f"Refused: cannot write {path!r}. Only {sorted(_WRITABLE_FILES)} and paths "
            f"under {_WRITABLE_DIRS} are writable — vocabulary/, kg/, and review/ always "
            "require a human, per the guardrails in your system prompt."
        )
    if rel.endswith((".yaml", ".yml")):
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as exc:
            return f"Refused: {rel!r} would not be valid YAML ({exc}). Not written."
    full = project.root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    return f"Wrote {len(content)} bytes to {rel}."


def _export(
    project: Project,
    fmt: str = "html",
    output: str | None = None,
    default_view: str = "tech",
    node_type: str | None = None,
    around: str | None = None,
    hops: int = 1,
) -> str:
    """Render the KG to a visualisation/graph-tool file. Not gated like
    `write_file` — this only ever produces a new derived artefact (e.g.
    `kg/capability-map.html`), never mutates `kg/capability-map.json` or
    anything else that requires a human decision."""
    from epistemic_agent.export.viewer import run_export

    if not project.kg_path.exists():
        return _NO_KG.format(path=project.kg_path)

    result = run_export(
        project, fmt=fmt, output=output, default_view=default_view,
        node_type=node_type, around=around, hops=hops,
    )
    if "error" in result:
        return result["error"]
    lines = list(result["notes"])
    lines.append(f"Wrote {result['output_path']} ({result['size_bytes'] / 1024:.0f} KB)")
    lines.append(result["tip"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# OpenAI-compatible function-calling tools


def build_openai_tools(project: Project) -> tuple[list[dict], dict]:
    """Build the agent's tool set in OpenAI function-calling shape.

    The schemas here are written out by hand rather than inferred from type
    hints. The tool set must stay exactly the guardrail whitelist described
    in this module's docstring — see `test_build_openai_tools_...` in
    `tests/test_smoke.py` for the guarantee this is pinned by.

    Returns `(specs, dispatch)`: `specs` is `[{"name", "description",
    "parameters"}, ...]` ready to wrap as `{"type": "function", "function":
    spec}`; `dispatch` maps each tool name to a callable taking the parsed
    argument dict as keyword arguments and returning a string.
    """
    specs = [
        {
            "name": "orient_state",
            "description": (
                "Summarise the current graph state: node/edge counts, health "
                "signals, and the pending review queue size. Always call this "
                "first in a new conversation, and after any long gap, before "
                "deciding what to do next."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "read_skill",
            "description": (
                "Read a skill playbook's full text from skills/<name>.md. Pull in "
                "exactly the playbook needed for the current step instead of "
                "assuming you already know it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": 'skill file stem, e.g. "orient-state", "bootstrap-instance", "seed-vocabulary".',
                    }
                },
                "required": ["name"],
            },
        },
        {
            "name": "run_bootstrap",
            "description": (
                "Run scripts/bootstrap.py and return its stats report. Works whether "
                "the instance has the generic config-driven script or a hand-written "
                "one. Read the counts it prints as honest gaps in the source data, "
                "not bugs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "out": {
                        "type": "string",
                        "description": "where to write the ingest payload, relative to the instance root.",
                    }
                },
                "required": [],
            },
        },
        {
            "name": "ingest_payload",
            "description": (
                "Write an ingest payload into the knowledge graph — the only write "
                "path into the KG."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "path to the payload JSON, resolved from the current working directory.",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "ingest even if every declared source file is unchanged since the last ingest.",
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "vocab_draft",
            "description": (
                "Cluster ungrounded labels into a draft vocabulary for human review. "
                "Deterministic clustering only — never decides whether a group is "
                "real, and never writes to vocabulary/."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "payload": {
                        "type": "string",
                        "description": "bootstrap payload JSON path to cluster (a fresh instance's first pass).",
                    },
                    "from_suggestions": {
                        "type": "boolean",
                        "description": "cluster kg/vocabulary-suggestions.md instead (topping up an established instance).",
                    },
                    "fuzzy_threshold": {
                        "type": "number",
                        "description": "similarity cutoff for the fuzzy clustering pass.",
                    },
                },
                "required": [],
            },
        },
        {
            "name": "merge_dry_run",
            "description": (
                "Run the configured merge strategies without writing anything, and "
                "return the report. Always call this — and show the report to the "
                "human — before ever calling merge_apply."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "merge_apply",
            "description": (
                "Apply the configured merge strategies to the knowledge graph. Only "
                "call this after merge_dry_run's report has been shown to the human "
                "and they have explicitly said to proceed."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "view",
            "description": "Run an analysis view over the capability map and return it as JSON.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": ["coverage", "duplicates", "gaps", "experts", "risk"],
                        "description": "which view to run.",
                    },
                    "query": {
                        "type": "string",
                        "description": 'required for "experts" — a technology or capability to search for.',
                    },
                    "by": {
                        "type": "string",
                        "description": 'for "coverage" — "shard" or "domain".',
                    },
                    "limit": {
                        "type": "integer",
                        "description": "max rows to return (0 for no limit).",
                    },
                },
                "required": ["name"],
            },
        },
        {
            "name": "review_status",
            "description": (
                "List pending review-queue items and counts by type. Read-only — "
                "resolving an item is a human action, there is no tool for it."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "read_file",
            "description": "Read a local file's contents. No path restriction.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "file path, absolute or relative to the current working directory.",
                    }
                },
                "required": ["path"],
            },
        },
        {
            "name": "fetch_url",
            "description": "Fetch a URL's contents over HTTP(S).",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "the URL to fetch."}},
                "required": ["url"],
            },
        },
        {
            "name": "write_file",
            "description": (
                "Write a file — only for instance setup (config/project.yaml, "
                "schema/kg-schema.yaml, scripts/bootstrap.py, data/raw/*), never "
                "for anything a human must decide. Everything else (vocabulary/, "
                "kg/, review/) is refused. Show the content to the human before "
                "calling this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "path relative to the instance root."},
                    "content": {
                        "type": "string",
                        "description": "full file content to write (replaces the file, not a diff).",
                    },
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "export",
            "description": (
                "Render the graph to a visualisation or graph-analysis file (default: "
                "HTML). Only ever produces a new derived file — never touches "
                "kg/capability-map.json itself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fmt": {
                        "type": "string",
                        "enum": ["html", "graphml", "gexf", "cypher", "dot"],
                        "description": "output format, default html.",
                    },
                    "output": {"type": "string", "description": "output path; defaults to kg/capability-map.<ext>."},
                    "default_view": {
                        "type": "string",
                        "description": 'for html — "tech" | "team" | "domain" | "full".',
                    },
                    "node_type": {
                        "type": "string",
                        "description": "comma-separated list to keep only certain node types.",
                    },
                    "around": {
                        "type": "string",
                        "description": "centre the export on one node (id or a resolvable label/tech name).",
                    },
                    "hops": {"type": "integer", "description": "how many edges to expand from around."},
                },
                "required": [],
            },
        },
    ]

    dispatch = {
        "orient_state": lambda **kw: _orient_state(project),
        "read_skill": lambda **kw: _read_skill(project, kw["name"]),
        "run_bootstrap": lambda **kw: _run_bootstrap(
            project, out=kw.get("out", "payload.bootstrap.json")
        ),
        "ingest_payload": lambda **kw: _ingest_payload(
            project, kw["path"], force=kw.get("force", False)
        ),
        "vocab_draft": lambda **kw: _vocab_draft(
            project,
            payload=kw.get("payload"),
            from_suggestions=kw.get("from_suggestions", False),
            fuzzy_threshold=kw.get("fuzzy_threshold", 0.90),
        ),
        "merge_dry_run": lambda **kw: _merge_dry_run(project),
        "merge_apply": lambda **kw: _merge_apply(project),
        "view": lambda **kw: _view(
            project,
            kw["name"],
            query=kw.get("query", ""),
            by=kw.get("by", "shard"),
            limit=kw.get("limit", 20),
        ),
        "review_status": lambda **kw: _review_status(project),
        "read_file": lambda **kw: _read_file(kw["path"]),
        "fetch_url": lambda **kw: _fetch_url(kw["url"]),
        "write_file": lambda **kw: _write_file(project, kw["path"], kw["content"]),
        "export": lambda **kw: _export(
            project,
            fmt=kw.get("fmt", "html"),
            output=kw.get("output"),
            default_view=kw.get("default_view", "tech"),
            node_type=kw.get("node_type"),
            around=kw.get("around"),
            hops=kw.get("hops", 1),
        ),
    }

    return specs, dispatch
