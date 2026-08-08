"""capmap — enterprise technology capability map CLI.

    capmap init <name>        Scaffold a new instance
    capmap stats              Node/edge counts by type
    capmap health             Quality + capability-map signals
    capmap merge [--dry-run]  Run the configured merge strategies
    capmap review             Open the review queue TUI
    capmap ground <label>     Resolve one label against the vocabulary
    capmap vocab              List vocabulary shards
    capmap vocab-draft        Cluster ungrounded labels into a draft vocabulary
    capmap view <name>        Coverage / duplicates / gaps / experts / risk
    capmap ingest <payload>   Write a payload into the KG
    capmap agent              Start an interactive, tool-using agent session

Every command resolves its paths from `config/project.yaml`, found by walking
up from the working directory, through one resolver (`Project`). A script that
hard-codes its own default path or reads a config key that moved will silently
fall back to that default instead of failing — the fix is one resolver that
every command shares, so a missing or misconfigured key fails loudly instead
of resolving to a plausible-looking wrong answer.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from epistemic_agent.project import Project, find_project

app = typer.Typer(help="epistemic-agent CLI", add_completion=False, no_args_is_help=True)
console = Console()

_TEMPLATE_DIR = Path(__file__).parent / "template"


# ---------------------------------------------------------------------------
# capmap init


@app.command()
def init(
    name: str = typer.Argument(..., help="Name for the new instance"),
    target: Optional[Path] = typer.Option(None, "--target", "-t", help="Parent directory"),
):
    """Scaffold a new capability-map instance from the template."""
    import shutil

    dest = (target or Path.cwd()) / name
    if dest.exists():
        console.print(f"[red]Directory already exists:[/red] {dest}")
        raise typer.Exit(1)
    if not _TEMPLATE_DIR.exists():
        console.print(f"[red]Template directory not found:[/red] {_TEMPLATE_DIR}")
        raise typer.Exit(1)

    shutil.copytree(_TEMPLATE_DIR, dest)
    for f in dest.rglob("*"):
        if f.is_file() and f.suffix in (".yaml", ".yml", ".md"):
            text = f.read_text()
            if "{{instance_name}}" in text:
                f.write_text(text.replace("{{instance_name}}", name))

    console.print(f"[green]Created:[/green] {dest}")
    console.print(f"\n  cd {dest}\n  claude")
    console.print("\n[dim]Describe your organisation to the agent — it takes it from there.[/dim]")


# ---------------------------------------------------------------------------
# capmap stats


@app.command()
def stats():
    """Print node and edge counts by type."""
    from epistemic_agent.kg.store import GraphStore

    proj = find_project()
    store = GraphStore.load(proj.kg_path, proj.schema_path)
    s = store.get_statistics()
    console.print(
        f"\n[bold]{proj.name}[/bold] — "
        f"{s['total_nodes']} nodes, {s['total_edges']} edges"
    )

    from rich.table import Table

    t = Table(box=None)
    t.add_column("node type")
    t.add_column("count", justify="right")
    for k, v in s["node_types"].items():
        t.add_row(k, str(v))
    console.print(t)

    e = Table(box=None)
    e.add_column("edge type")
    e.add_column("count", justify="right")
    for k, v in s["edge_types"].items():
        e.add_row(k, str(v))
    console.print(e)

    conn = s["connectivity"]
    console.print(
        f"Connectivity: {conn['connected_nodes']} connected, {conn['isolated_nodes']} isolated"
    )
    for w in s["schema_warnings"]:
        console.print(f"[yellow]SCHEMA[/yellow] {w}")


# ---------------------------------------------------------------------------
# capmap health


@app.command()
def health(
    output: Optional[Path] = typer.Option(None, "--output", "-o"),
    stale_days: int = typer.Option(365, "--stale-days"),
    json_out: bool = typer.Option(False, "--json", help="Print the manifest as JSON"),
):
    """Generate the KG health manifest."""
    from epistemic_agent.health.manifest import generate_manifest, print_summary

    proj = find_project()
    m = generate_manifest(
        kg_path=proj.kg_path,
        output_path=output or proj.health_path,
        stale_days=stale_days,
        semantic_types=proj.semantic_types(),
        vocabulary=proj.vocabulary(),
    )
    if json_out:
        console.print_json(m.model_dump_json())
    else:
        print_summary(m)
        console.print(f"\n[dim]Manifest written to {output or proj.health_path}[/dim]")


# ---------------------------------------------------------------------------
# capmap merge


@app.command()
def merge(
    dry_run: bool = typer.Option(False, "--dry-run"),
    report: Optional[Path] = typer.Option(None, "--report"),
    max_rounds: int = typer.Option(5, "--max-rounds", help="Fixed-point iteration cap"),
):
    """Run the merge strategies configured in project.yaml.

    Merging is a closure, not a single pass: folding aliases onto their
    vocabulary label lets more ungrounded nodes fuzzy-match on the next round.
    Iterating here means one `capmap merge` is actually enough — otherwise the
    graph silently depends on how many times someone happened to run it.
    """
    from epistemic_agent.merge.strategies import run_merge

    proj = find_project()
    result = run_merge(proj, dry_run=dry_run, report_path=report, max_rounds=max_rounds)
    if "error" in result:
        console.print(f"[yellow]{result['error']}[/yellow]")
        raise typer.Exit(1)

    if result["violations"]:
        console.print(f"[yellow]{result['violations']} invariant violation(s)[/yellow] — see report")
    if result["hit_max_rounds"]:
        console.print(
            f"[yellow]Hit the {max_rounds}-round cap[/yellow] — the merge has not converged. "
            "Check the report for a strategy pair that keeps renaming the same nodes."
        )

    if dry_run:
        console.print(
            f"[yellow]DRY RUN[/yellow] — {result['merges']} merges planned over "
            f"{result['rounds']} round(s). Report: {result['report_path']}"
        )
        return

    mstats = result["merge_stats"]
    console.print(f"Merges applied: {result['merges']} over {result['rounds']} round(s)")
    console.print(f"Nodes: {result['before_nodes']} → {result['after_nodes']}")
    console.print(f"Edges: {result['before_edges']} → {result['after_edges']}")
    console.print(
        f"[dim]dropped {mstats.get('dropped_duplicates', 0)} duplicate edges, "
        f"{mstats.get('dropped_self_loops', 0)} self-loops; "
        f"{mstats.get('properties_asserted', 0)} properties set from the vocabulary[/dim]"
    )
    console.print(f"Report: {result['report_path']}")


# ---------------------------------------------------------------------------
# capmap review


@app.command()
def review(reviewer: str = typer.Option("", "--reviewer", "-r")):
    """Open the review queue TUI."""
    proj = find_project()
    from epistemic_agent.review.queue import ReviewQueue

    q = ReviewQueue(proj.review_dir)
    pending = q.pending_count()
    if pending == 0:
        console.print("[green]No pending review items.[/green]")
        return
    from epistemic_agent.review.tui import run_tui

    run_tui(review_dir=proj.review_dir, reviewer=reviewer)


@app.command("review-status")
def review_status():
    """Summarise the review queue without opening the TUI."""
    from collections import Counter

    from epistemic_agent.review.queue import ReviewQueue

    proj = find_project()
    q = ReviewQueue(proj.review_dir)
    items = q.list_items()
    by_type = Counter(i.source_type for i in items)
    console.print(f"\n[bold]{proj.name}[/bold] — {len(items)} pending review items")
    from rich.table import Table

    t = Table(box=None)
    t.add_column("source_type")
    t.add_column("count", justify="right")
    for k, v in by_type.most_common():
        t.add_row(k, str(v))
    console.print(t)
    for i in items[:5]:
        console.print(f"  [dim]{i.priority.score:.2f}[/dim] {i.prompt.title}")


# ---------------------------------------------------------------------------
# capmap ground / vocab


@app.command()
def ground(
    label: str = typer.Argument(..., help="Free-text label to resolve"),
    target_type: str = typer.Option("TechStack", "--type"),
    limit: int = typer.Option(5, "--limit"),
):
    """Resolve one label against the controlled vocabulary."""
    proj = find_project()
    vocab = proj.vocabulary()
    result = vocab.ground(label, limit=limit, target_type=target_type)
    colour = {"grounded": "green", "candidate": "yellow", "ungrounded": "red"}[result.status]
    console.print(f"\n[{colour}]{result.status}[/{colour}]  {label!r}  score={result.score:.3f}")
    from rich.table import Table

    t = Table(box=None)
    for c in ("term_id", "label", "shard", "score", "matched_on"):
        t.add_column(c)
    for c in result.candidates:
        t.add_row(c.term_id, c.label, c.shard, f"{c.score:.3f}", c.matched_on)
    console.print(t)


@app.command()
def vocab():
    """List the vocabulary shards in use."""
    proj = find_project()
    v = proj.vocabulary()
    from rich.table import Table

    t = Table(title=f"{v.term_count} terms across {len(v.shards)} shards", title_justify="left")
    for c in ("shard", "target", "terms", "label"):
        t.add_column(c)
    for s in v.list_shards():
        t.add_row(s["shard"], s.get("target_type", "?"), str(s["term_count"]), s["label"])
    console.print(t)


@app.command("vocab-draft")
def vocab_draft(
    payload: Optional[Path] = typer.Argument(
        None, help="Bootstrap payload JSON to cluster (omit with --from-suggestions)"
    ),
    from_suggestions: bool = typer.Option(
        False, "--from-suggestions", help="Cluster kg/vocabulary-suggestions.md instead"
    ),
    fuzzy_threshold: float = typer.Option(0.90, "--fuzzy-threshold"),
    no_fuzzy: bool = typer.Option(False, "--no-fuzzy", help="Exact clustering only, no fuzzy pass"),
    out: Optional[Path] = typer.Option(None, "--out", help="Draft output directory"),
):
    """Cluster ungrounded labels into a draft vocabulary for human review.

    Deterministic only (exact match + string-similarity clustering, hostnames
    excluded from fuzzy matching) — never writes to `vocabulary/` and never
    decides whether a group is real. See `skills/seed-vocabulary.md`.
    """
    from epistemic_agent.onto.draft import (
        build_vocab_draft,
        labels_from_payload,
        labels_from_suggestions_md,
        write_draft_files,
    )

    proj = find_project()
    if from_suggestions:
        if not proj.suggestions_path.exists():
            console.print(f"[yellow]No suggestions file at {proj.suggestions_path}[/yellow]")
            raise typer.Exit(1)
        labels = labels_from_suggestions_md(proj.suggestions_path.read_text())
    else:
        if payload is None:
            console.print("[red]Pass a payload JSON path, or use --from-suggestions[/red]")
            raise typer.Exit(1)
        labels = labels_from_payload(json.loads(payload.read_text()))

    if not labels:
        console.print("[green]Nothing to cluster.[/green]")
        return

    threshold = None if no_fuzzy else fuzzy_threshold
    drafts = build_vocab_draft(labels, fuzzy_threshold=threshold)
    out_dir = out or (proj.root / "kg" / "vocabulary-draft")
    written = write_draft_files(drafts, out_dir)

    for ntype, draft in drafts.items():
        console.print(f"{ntype}: {draft['raw_labels']} labels → {draft['clusters']} groups")
    for p in written:
        console.print(f"[green]Wrote[/green] {p}")
    console.print(
        "[dim]Draft only — not loaded as vocabulary. Review with "
        "skills/seed-vocabulary.md, then move accepted groups into "
        "vocabulary/*.yaml and delete the draft.[/dim]"
    )


# ---------------------------------------------------------------------------
# capmap show


@app.command()
def show(
    query: str = typer.Argument(..., help="节点 ID、技术名，或标签/路径的一部分"),
    json_out: bool = typer.Option(False, "--json"),
    all_teams: bool = typer.Option(False, "--all-teams"),
    limit: int = typer.Option(12, "--limit", help="每种关系列几个邻居"),
):
    """钻取单个节点：属性、关系、邻居分布、溯源。"""
    from epistemic_agent.analysis.inspect import (
        build_index,
        describe_node,
        render_node,
        resolve_node,
        vocabulary_hit_ids,
    )

    proj = find_project()
    vocab = proj.vocabulary()
    idx = build_index(proj.load_kg())
    hits, how = resolve_node(idx, query, vocab)

    if not hits:
        console.print(f"[red]找不到[/red] {query!r}")
        console.print("[dim]试试 `capmap stats` 看有哪些类型，或用标签的一部分做子串匹配。[/dim]")
        raise typer.Exit(1)

    if len(hits) > 1:
        console.print(f"[yellow]{query!r} 匹配到 {len(hits)} 个节点[/yellow]（{how}）：")
        from rich.table import Table

        marked = vocabulary_hit_ids(idx, query, vocab)
        t = Table(box=None)
        for c in ("", "id", "类型", "标签"):
            t.add_column(c, overflow="fold")
        for nid in hits[:25]:
            t.add_row(
                "★" if nid in marked else "",
                nid,
                idx.nodes[nid]["type"],
                idx.label(nid),
            )
        console.print(t)
        if len(hits) > 25:
            console.print(f"[dim]… 另 {len(hits) - 25} 个[/dim]")
        console.print("[dim]★ = 词表接地命中。用完整 id 再查一次看单个节点详情。[/dim]")
        return

    info = describe_node(idx, hits[0], neighbour_limit=limit)
    if json_out:
        console.print_json(json.dumps(info, ensure_ascii=False))
        return
    console.print(f"[dim]（{how}）[/dim]")
    render_node(console, info, show_all_teams=all_teams)


# ---------------------------------------------------------------------------
# capmap view


@app.command()
def view(
    name: str = typer.Argument(..., help="coverage | duplicates | gaps | experts | risk"),
    query: str = typer.Option("", "--query", "-q", help="For `experts`: what to search for"),
    by: str = typer.Option("shard", "--by", help="For `coverage`: shard | domain"),
    limit: int = typer.Option(20, "--limit", help="0 for no limit"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Run an analysis view over the capability map."""
    from epistemic_agent.analysis import views as V

    proj = find_project()
    idx = V.GraphIndex(proj.load_kg())
    lim = None if limit == 0 else limit

    if name == "coverage":
        rows = V.view_coverage(idx, by=by)
        if json_out:
            console.print_json(json.dumps(rows, ensure_ascii=False))
            return
        if by == "domain":
            V.render_table(
                console, "领域覆盖", ["领域", "仓库", "团队", "技术数"],
                [[r["group"], r["repos"], r["teams"], r["distinct_tech"]] for r in rows], lim,
            )
        else:
            V.render_table(
                console, "技术栈覆盖（按词表分片）",
                ["分片", "技术数", "仓库", "团队", "最常用"],
                [[r["group"], r["distinct_tech"], r["repos"], r["teams"], ", ".join(r["top"])]
                 for r in rows], lim,
            )

    elif name == "duplicates":
        rows = V.view_duplicates(idx)
        if json_out:
            console.print_json(json.dumps(rows, ensure_ascii=False))
            return
        V.render_table(
            console, "疑似重复投入（跨团队、同领域、栈高度重合）",
            ["关系", "领域", "A", "B", "团队", "重合度", "共同技术"],
            [[r["relation"], r["domain"], r["label_a"], r["label_b"],
              f"{r['team_a']} / {r['team_b']}",
              f"{r['jaccard']:.2f}", ", ".join(r["shared"][:6])] for r in rows], lim,
        )
        nfork = sum(1 for r in rows if r["relation"] != "independent")
        console.print(
            f"[dim]重合 ≠ 重复。independent 才值得看；{nfork}/{len(rows)} 行是同名或 fork/副本，"
            "已排到后面。任何一行都要人工判断后才写 DUPLICATES 边。[/dim]"
        )

    elif name == "gaps":
        rows = V.view_gaps(idx, proj.vocabulary())
        if json_out:
            console.print_json(json.dumps(rows, ensure_ascii=False))
            return
        V.render_table(
            console, "能力缺口（no-repo = 词表里有但没人做；single-repo = 只有 1 个仓库）",
            ["分片", "术语", "状态", "仓库数"],
            [[r["shard"], r["label"], r["status"], r["repos"]] for r in rows], lim,
        )
        skipped = V.count_ungrounded_singletons(idx)
        console.print(
            f"[dim]另有 {skipped} 个未接地的一次性标签未列入——那是词表建议，"
            f"见 {proj.suggestions_path.name}，不是能力缺口。"
            "\nno-repo 只在词表完整且摄取完整时才等于「没人做」，两者都从来不完全成立。[/dim]"
        )
        for ttype, n in V.unpopulated_target_types(idx, proj.vocabulary()).items():
            console.print(
                f"[yellow]注意[/yellow] 图里没有任何 {ttype} 节点，词表中 {n} 个 "
                f"{ttype} 术语已整体排除出本视图——这说明抽取 {ttype} 的那一步还没跑，"
                "不能读作「这些能力没人具备」。"
            )

    elif name == "experts":
        if not query:
            console.print("[red]--query is required for the experts view[/red]")
            raise typer.Exit(1)
        res = V.view_experts(idx, query, proj.vocabulary(), limit=lim or 10_000)
        if json_out:
            console.print_json(json.dumps(res, ensure_ascii=False))
            return
        console.print(
            f"\n[bold]{res['query']}[/bold] → {res['resolved']}  ({res['total']} 个仓库)"
        )
        V.render_table(
            console, "谁做过", ["仓库", "团队", "档位", "复用度", "最后提交", "可以拿走什么"],
            [[h["path"] or h["label"], h["team"], h["tier"], h["reuse"], h["last_commit"],
              (h["value"] or "")[:60]] for h in res["hits"]], None,
        )

    elif name == "risk":
        res = V.view_risk(idx)
        if json_out:
            console.print_json(json.dumps(res, ensure_ascii=False))
            return
        V.render_table(
            console, "单团队掌握的能力（bus factor = 1）",
            ["能力/技术", "唯一团队", "仓库数"],
            [[r["label"], r["team"], r["repos"]] for r in res["single_team"]], lim,
        )
        V.render_table(
            console, "内部系统依赖集中度", ["内部系统", "依赖仓库", "涉及团队"],
            [[r["label"], r["repos"], r["teams"]] for r in res["internal_lockin"]], lim,
        )

    else:
        console.print(f"[red]Unknown view {name!r}[/red] — "
                      "coverage | duplicates | gaps | experts | risk")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# capmap export


@app.command()
def export(
    fmt: str = typer.Option("html", "--format", "-f",
                            help="html | graphml | gexf | cypher | dot"),
    output: Optional[Path] = typer.Option(None, "--output", "-o"),
    default_view: str = typer.Option("tech", "--view",
                                     help="HTML 默认视图：tech | team | domain | full"),
    node_type: Optional[str] = typer.Option(None, "--node-type",
                                            help="只导出这些类型，逗号分隔"),
    around: Optional[str] = typer.Option(None, "--around",
                                         help="只导出某节点周围的子图（节点 ID 或技术名）"),
    hops: int = typer.Option(1, "--hops", help="--around 的跳数"),
    keep_sources: bool = typer.Option(False, "--keep-sources",
                                      help="保留 _sources（体积大，可视化时是噪声）"),
):
    """把图谱导出成可视化 / 图分析工具能吃的格式。"""
    from epistemic_agent.export.viewer import run_export, view_summary

    proj = find_project()
    result = run_export(
        proj, fmt=fmt, output=output, default_view=default_view,
        node_type=node_type, around=around, hops=hops, keep_sources=keep_sources,
    )
    if "error" in result:
        console.print(f"[red]{result['error']}[/red]")
        raise typer.Exit(1)

    for note in result["notes"]:
        console.print(f"[dim]{note}[/dim]")

    if fmt == "html":
        rows = view_summary(result["kg"])
        from rich.table import Table

        t = Table(title="内置视图", title_justify="left", box=None)
        for c in ("key", "名称", "节点", "边", "说明"):
            t.add_column(c, overflow="fold")
        for r in rows:
            t.add_row(r["key"], r["name"], str(r["nodes"]), str(r["edges"]), r["desc"])
        console.print(t)

    console.print(
        f"[green]已写入[/green] {result['output_path']}  ({result['size_bytes'] / 1024:.0f} KB)"
    )
    console.print(f"[dim]{result['tip']}[/dim]")


# ---------------------------------------------------------------------------
# capmap ingest


@app.command()
def ingest(
    payload: Path = typer.Argument(..., help="Path to an entities JSON payload"),
    ground_first: bool = typer.Option(True, "--ground/--no-ground",
                                      help="Resolve labels before writing"),
    force: bool = typer.Option(False, "--force",
                               help="Ingest even if every source file is unchanged"),
):
    """Write an entities payload into the KG (the only write path)."""
    from epistemic_agent.ingest.document import (
        ground_payload,
        ingest_entities,
        mark_processed,
        unprocessed_files,
        write_vocabulary_suggestions,
    )
    from epistemic_agent.review.queue import ReviewQueue

    proj = find_project()
    data = json.loads(payload.read_text())

    # The manifest exists for exactly this: a hash check that is written but
    # never called is worse than useless, because re-running a bootstrap would
    # silently re-add every node a merge had folded away.
    declared = [Path(p) for p in data.get("source_files", [])]
    if declared and not force:
        pending = unprocessed_files(declared, proj.manifest_path)
        if not pending:
            console.print(
                f"[green]Nothing to do[/green] — all {len(declared)} source files are "
                "unchanged since the last ingest."
            )
            console.print("[dim]Use --force to ingest anyway.[/dim]")
            return
        console.print(f"[dim]{len(pending)}/{len(declared)} source files changed[/dim]")

    if ground_first:
        q = ReviewQueue(proj.review_dir)
        gstats = ground_payload(data, proj.vocabulary(), queue=q, project_name=proj.name)
        write_vocabulary_suggestions(data.get("_vocabulary_suggestions", []),
                                     proj.suggestions_path)
        gmsg = (
            f"Grounding: {gstats['grounded']} grounded, {gstats['candidate']} candidates, "
            f"{gstats['ungrounded']} ungrounded, {gstats['skipped']} structural"
        )
        if gstats.get("requeued"):
            gmsg += f" ({gstats['requeued']} already in the review queue, not re-asked)"
        console.print(gmsg)

    node_ids, s = ingest_entities(
        data, proj.kg_path, proj.schema_path, proj.manifest_path, proj.changelog_path
    )
    for sf in data.get("source_files", []):
        mark_processed(sf, proj.manifest_path, node_ids)

    msg = (
        f"Ingest: +{s['nodes_added']} nodes, ~{s['nodes_updated']} updated, "
        f"+{s['edges_added']} edges"
    )
    if s.get("redirected"):
        msg += f", {s['redirected']} redirected to post-merge IDs"
    console.print(msg)
    for w in s.get("schema_warnings") or []:
        console.print(f"[yellow]SCHEMA[/yellow] {w}")


# ---------------------------------------------------------------------------
# capmap agent


@app.command()
def agent(
    model: Optional[str] = typer.Option(
        None, "--model", help="Override the model (default depends on --backend)"
    ),
    backend: Optional[str] = typer.Option(
        None,
        "--backend",
        help="anthropic | openai. Default: openai (or $CAPMAP_AGENT_BACKEND if set).",
    ),
):
    """Start an interactive, tool-using agent session for this instance.

    A standalone conversational loop — no Claude Code dependency. It reads the
    same CLAUDE.md a human-operated Claude Code session would, and calls the
    deterministic tools in `epistemic_agent.agent.tools` itself. Vocabulary
    finalisation, review-queue resolution, and DUPLICATES edges stay
    human-only: there is no tool for them, on either backend.

    Two backends: `openai` (default) goes through any endpoint that speaks
    the OpenAI `/chat/completions` wire format — OpenAI itself, or a gateway
    your organisation runs in front of it (Portkey, LiteLLM, Azure OpenAI,
    Bedrock, ...) — see `epistemic_agent.agent.backends.openai_backend` for
    the required env vars and your provider's own onboarding docs for the
    values. `anthropic` calls the Claude API directly.
    """
    from epistemic_agent.agent.runtime import run_repl

    try:
        run_repl(find_project(), model=model, backend=backend)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    app()
