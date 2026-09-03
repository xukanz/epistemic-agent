"""Smoke tests over the pieces that were actually wrong at some point.

Each of these encodes a bug found while bootstrapping the first instance, so
they are regression tests rather than coverage decoration.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from epistemic_agent.agent.tools import (
    _export,
    _fetch_url,
    _ingest_payload,
    _merge_apply,
    _merge_dry_run,
    _orient_state,
    _read_file,
    _read_skill,
    _review_status,
    _subgraph,
    _vocab_draft,
    _view,
    _write_file,
    build_openai_tools,
)
from epistemic_agent.analysis import views as V
from epistemic_agent.bootstrap.generic import run_generic_bootstrap
from epistemic_agent.ingest.document import ground_payload, ingest_entities
from epistemic_agent.kg.store import GraphStore, edge_id
from epistemic_agent.merge.canonical import (
    canonical_repo_id,
    is_noise,
    normalise_tech_label,
    slugify,
    split_multi_value,
)
from epistemic_agent.merge.strategies import (
    apply_merges,
    build_merge_map,
    strategies_from_config,
)
from epistemic_agent.onto.client import LocalVocabulary
from epistemic_agent.onto.draft import (
    build_vocab_draft,
    labels_from_suggestions_md,
    write_draft_files,
)
from epistemic_agent.project import Project

# ---------------------------------------------------------------------------
# Normalisation


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("LangGraph", "langgraph"),
        ("Next.js 14", "next.js"),
        ("react18", "react"),
        ("R 4.4.1", "r"),
        ("Node 22+", "node"),
        ("spring-boot3", "spring-boot"),
        ("AWS CDK(Python)", "aws cdk"),
        ("torch(CPU)", "torch"),
        # Trailing digits that are part of the name, not a version.
        ("boto3", "boto3"),
        ("S3", "s3"),
        ("oauth2", "oauth2"),
        ("a2a", "a2a"),
    ],
)
def test_normalise_tech_label(raw, expected):
    assert normalise_tech_label(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("anthropic/openai", ["anthropic", "openai"]),
        ("FastAPI+sse-starlette", ["FastAPI", "sse-starlette"]),
        ("Maven/Jacoco/Sonar", ["Maven", "Jacoco", "Sonar"]),
        # A separator inside a parenthetical qualifies the name, not splits it.
        ("R(dplyr/ggplot2)", ["R"]),
        # Scoped packages and URLs are single values.
        ("@modelcontextprotocol/sdk", ["@modelcontextprotocol/sdk"]),
        ("C++", ["C++"]),
    ],
)
def test_split_multi_value(raw, expected):
    assert split_multi_value(raw) == expected


def test_repo_id_is_injective_over_paths():
    """Two repos differing only in `_` vs `-` must not collide."""
    a = canonical_repo_id("team/data_pipeline")
    b = canonical_repo_id("team/data-pipeline")
    assert a != b


def test_slugify_falls_back_for_non_ascii_labels():
    """A Chinese-only label must not slugify to the empty string."""
    a, b = slugify("证据不足"), slugify("重试与退避")
    assert a and b and a != b


def test_is_noise():
    assert is_noise("证据不足")
    assert is_noise("generate_schema.py")
    assert not is_noise("langgraph")


# ---------------------------------------------------------------------------
# Vocabulary


def test_vocabulary_loads():
    v = LocalVocabulary()
    assert v.term_count > 150
    assert len(v.shards) >= 10


def test_grounding_is_target_type_aware():
    """`react` is the framework to a TechStack node and ReAct to a Pattern."""
    v = LocalVocabulary()
    tech = v.ground("react", target_type="TechStack")
    pattern = v.ground("react", target_type="Pattern")
    assert tech.best.term_id == "fe:react"
    assert pattern.best.term_id == "ap:react-loop"


def test_grounding_bands():
    v = LocalVocabulary()
    assert v.ground("LangGraph").status == "grounded"
    assert v.ground("zzzz-not-a-real-technology").status == "ungrounded"


# ---------------------------------------------------------------------------
# Generic bootstrap


def test_generic_bootstrap_maps_fields_and_splits_multi_value_tech(tmp_path):
    """A flat, one-record-per-repo source should need only a `bootstrap:`
    field-mapping config, not a hand-written script — this is the shape
    acme-corp's bootstrap.py used to hard-code."""
    (tmp_path / "seed.yaml").write_text(
        "items:\n"
        "  - repo_path: team-a/svc\n"
        "    squad: team-a\n"
        "    stack: LangGraph, FastAPI/Starlette\n"
    )
    config = {
        "bootstrap": {
            "format": "yaml",
            "file": "seed.yaml",
            "list_key": "items",
            "fields": {"path": "repo_path", "team": "squad", "tech": "stack"},
        }
    }
    payload, report = run_generic_bootstrap(config, tmp_path)
    tech_labels = {n["label"] for n in payload["nodes"] if n["type"] == "TechStack"}
    assert tech_labels == {"LangGraph", "FastAPI", "Starlette"}
    team = next(n for n in payload["nodes"] if n["type"] == "Team")
    assert team["kind"] == "group"
    assert report["repos_without_domain"] == 1


def test_generic_bootstrap_marks_inferred_team_kind_unknown(tmp_path):
    """No `team` field mapped means the team name came from the path, not a
    deliberate assignment — must not silently claim `kind: group`, the same
    guessing acme-gitlab-agents' bootstrap.py refuses to do."""
    (tmp_path / "seed.yaml").write_text("items:\n  - path: team-a/svc\n")
    config = {"bootstrap": {"file": "seed.yaml", "list_key": "items"}}
    payload, report = run_generic_bootstrap(config, tmp_path)
    team = next(n for n in payload["nodes"] if n["type"] == "Team")
    assert team["kind"] == "unknown"
    assert report["repos_without_team_field"] == 1


# ---------------------------------------------------------------------------
# Vocabulary draft clustering


def test_vocab_draft_clusters_case_variants_via_exact_normalisation():
    labels = [
        {"label": "Atlas", "type": "TechStack", "node_id": "a"},
        {"label": "ATLAS", "type": "TechStack", "node_id": "b"},
        {"label": "atlas", "type": "TechStack", "node_id": "c"},
    ]
    drafts = build_vocab_draft(labels)
    terms = drafts["TechStack"]["terms"]
    assert len(terms) == 1
    assert set(terms[0]["_raw_variants"]) == {"Atlas", "ATLAS", "atlas"}


def test_vocab_draft_fuzzy_merges_near_duplicate_spellings():
    """A one-character typo survives the exact layer (different normalised
    forms) but should still land in one group via the fuzzy pass."""
    labels = [
        {"label": "Kubernetes", "type": "TechStack", "node_id": "a"},
        {"label": "Kubernets", "type": "TechStack", "node_id": "b"},
    ]
    drafts = build_vocab_draft(labels, fuzzy_threshold=0.90)
    terms = drafts["TechStack"]["terms"]
    assert len(terms) == 1
    assert set(terms[0]["_raw_variants"]) == {"Kubernetes", "Kubernets"}


def test_vocab_draft_never_fuzzy_merges_hostnames():
    """`sso.acme.example` and `app.acme.example` score well above this
    threshold on token_sort_ratio and would merge if not excluded —
    docs/new-instance.md calls this out as the mistake to avoid: hostnames
    are identifiers, not labels."""
    labels = [
        {"label": "sso.acme.example", "type": "InternalSystem", "node_id": "a"},
        {"label": "app.acme.example", "type": "InternalSystem", "node_id": "b"},
    ]
    drafts = build_vocab_draft(labels, fuzzy_threshold=0.75)
    terms = drafts["InternalSystem"]["terms"]
    assert len(terms) == 2
    assert all(t["_hostname"] for t in terms)


def test_vocab_draft_writes_outside_vocabulary_dir_and_is_not_loaded(tmp_path):
    """A draft must never be picked up by LocalVocabulary's `*.yaml` glob
    before a human has reviewed it."""
    drafts = build_vocab_draft([{"label": "Atlas", "type": "TechStack", "node_id": "a"}])
    out_dir = tmp_path / "kg" / "vocabulary-draft"
    written = write_draft_files(drafts, out_dir)
    assert written and all(p.parent == out_dir for p in written)

    vocab_dir = tmp_path / "vocabulary"
    vocab_dir.mkdir()
    assert LocalVocabulary(shard_dirs=[vocab_dir]).term_count == 0


def test_labels_from_suggestions_md_parses_bullet_lines():
    text = (
        "# Vocabulary suggestions\n\n## 2026-08-07\n\n"
        "- `yq` — TechStack (tech-yq)\n"
    )
    assert labels_from_suggestions_md(text) == [
        {"label": "yq", "type": "TechStack", "node_id": "tech-yq"}
    ]


# ---------------------------------------------------------------------------
# Store


def test_edge_ids_are_derived_so_ingest_is_idempotent(tmp_path):
    assert edge_id("a", "USES_TECH", "b") == "a__USES_TECH__b"
    store = GraphStore()
    store.add_edge(None, "USES_TECH", "a", "b")
    store.add_edge(None, "USES_TECH", "a", "b")
    assert len(store.edges) == 1


def test_add_node_does_not_clobber_with_empty_values():
    store = GraphStore()
    store.add_node("n1", "Repo", name="svc", path="t/svc")
    store.add_node("n1", "Repo", name="", path="t/svc", tier="A")
    props = store.nodes["n1"]["properties"]
    assert props["name"] == "svc"
    assert props["tier"] == "A"


# ---------------------------------------------------------------------------
# Ingest + grounding


def _schema(tmp_path: Path) -> Path:
    p = tmp_path / "schema.yaml"
    p.write_text(
        "nodes:\n  Repo: {}\n  TechStack: {}\nedges:\n  USES_TECH: {}\n"
    )
    return p


def test_grounding_prefers_label_over_raw_label(tmp_path):
    """`raw_label` holds the pre-split source string; grounding on it resolves
    the OpenAI half of `OpenAI/Anthropic` against Anthropic."""
    payload = {
        "nodes": [
            {"id": "tech-openai", "type": "TechStack",
             "label": "OpenAI", "raw_label": "OpenAI/Anthropic"},
        ]
    }
    ground_payload(payload, LocalVocabulary())
    assert payload["nodes"][0]["term_id"] == "llm:openai"


def test_ingest_is_idempotent(tmp_path):
    schema = _schema(tmp_path)
    kg = tmp_path / "kg.json"
    payload = {
        "source_files": [],
        "nodes": [
            {"id": "repo-a", "type": "Repo", "name": "a", "path": "t/a"},
            {"id": "tech-langgraph", "type": "TechStack", "label": "LangGraph"},
        ],
        "edges": [
            {"type": "USES_TECH", "source": "repo-a", "target": "tech-langgraph"}
        ],
    }
    for _ in range(2):
        ingest_entities(
            json.loads(json.dumps(payload)), kg, schema,
            tmp_path / "manifest.json", tmp_path / "changelog.md",
        )
    data = json.loads(kg.read_text())
    assert len(data["nodes"]) == 2
    assert len(data["edges"]) == 1


# ---------------------------------------------------------------------------
# Merge


def test_alias_merge_canonical_comes_from_term_id():
    """Shortest-ID selection would fold `mongodb` into `beanie`."""
    kg = {
        "nodes": [
            {"id": "tech-mongodb", "type": "TechStack",
             "properties": {"label": "mongodb", "term_id": "sr:mongodb"}},
            {"id": "tech-beanie", "type": "TechStack",
             "properties": {"label": "beanie", "term_id": "sr:mongodb"}},
        ],
        "edges": [],
    }
    cfg = {"merge": {"strategies": [
        {"type": "alias", "node_type": "TechStack", "id_prefix": "tech-"}
    ]}}
    merge, _, props = build_merge_map(kg, strategies_from_config(cfg, LocalVocabulary()))
    assert merge == {"tech-beanie": "tech-mongodb"}
    # The vocabulary's own name wins, not whichever alias was created first.
    assert props["tech-mongodb"]["label"] == "MongoDB"


def test_natural_key_guard_reports_and_never_merges():
    kg = {
        "nodes": [
            {"id": "repo-1", "type": "Repo", "properties": {"path": "t/x"}},
            {"id": "repo-2", "type": "Repo", "properties": {"path": "t/x"}},
        ],
        "edges": [],
    }
    cfg = {"merge": {"strategies": [
        {"type": "natural_key", "node_type": "Repo", "key_prop": "path"}
    ]}}
    merge, notes, _ = build_merge_map(kg, strategies_from_config(cfg))
    assert merge == {}
    assert any(k.startswith("!") for k in notes)


def test_apply_merges_repoints_edges_and_drops_duplicates():
    kg = {
        "nodes": [
            {"id": "a", "type": "TechStack", "properties": {}},
            {"id": "b", "type": "TechStack", "properties": {}},
            {"id": "r", "type": "Repo", "properties": {}},
        ],
        "edges": [
            {"id": "e1", "type": "USES_TECH", "source": "r", "target": "a"},
            {"id": "e2", "type": "USES_TECH", "source": "r", "target": "b"},
        ],
    }
    out = apply_merges(kg, {"b": "a"}, {})
    assert len(out["edges"]) == 1
    assert out["_merge_stats"]["dropped_duplicates"] == 1


# ---------------------------------------------------------------------------
# Views


def _tiny_graph() -> dict:
    return {
        "nodes": [
            {"id": "repo-x--svc", "type": "Repo",
             "properties": {"name": "svc", "path": "x/svc"}},
            {"id": "repo-y--svc", "type": "Repo",
             "properties": {"name": "service", "path": "y/svc"}},
            {"id": "team-x", "type": "Team", "properties": {"name": "x"}},
            {"id": "team-y", "type": "Team", "properties": {"name": "y"}},
            {"id": "domain-d", "type": "Domain", "properties": {"label": "D"}},
            *[
                {"id": f"tech-{t}", "type": "TechStack",
                 "properties": {"label": t, "term_id": f"x:{t}"}}
                for t in ("a", "b", "c")
            ],
        ],
        "edges": [
            {"id": "1", "type": "OWNED_BY", "source": "repo-x--svc", "target": "team-x"},
            {"id": "2", "type": "OWNED_BY", "source": "repo-y--svc", "target": "team-y"},
            {"id": "3", "type": "IN_DOMAIN", "source": "repo-x--svc", "target": "domain-d"},
            {"id": "4", "type": "IN_DOMAIN", "source": "repo-y--svc", "target": "domain-d"},
            *[
                {"id": f"u{i}{r}", "type": "USES_TECH", "source": r, "target": f"tech-{t}"}
                for i, t in enumerate("abc")
                for r in ("repo-x--svc", "repo-y--svc")
            ],
        ],
    }


def test_duplicates_flags_same_path_basename_as_fork():
    """Display names differed for five workshop forks; the paths did not."""
    idx = V.GraphIndex(_tiny_graph())
    rows = V.view_duplicates(idx)
    assert rows and rows[0]["relation"] == "fork-or-copy"


def test_gaps_excludes_target_types_with_no_nodes():
    """Zero Pattern nodes means the extractor did not run, not that nobody
    does RAG — those terms must not be reported as gaps."""
    idx = V.GraphIndex(_tiny_graph())
    vocab = LocalVocabulary()
    rows = V.view_gaps(idx, vocab)
    assert not any(r["shard"] == "agent-pattern" for r in rows)
    assert V.unpopulated_target_types(idx, vocab).get("Pattern", 0) > 0


def test_experts_resolves_query_through_the_vocabulary():
    kg = _tiny_graph()
    kg["nodes"].append(
        {"id": "tech-langgraph", "type": "TechStack",
         "properties": {"label": "LangGraph", "term_id": "af:langgraph"}}
    )
    kg["edges"].append(
        {"id": "z", "type": "USES_TECH", "source": "repo-x--svc", "target": "tech-langgraph"}
    )
    idx = V.GraphIndex(kg)
    res = V.view_experts(idx, "langgraph", LocalVocabulary())
    assert res["resolved"] == "LangGraph"
    assert res["total"] == 1


def test_reingest_after_merge_does_not_resurrect_merged_nodes(tmp_path):
    """The bug this guards: re-running a bootstrap re-added every node the
    merge had folded away, silently undoing it."""
    schema = _schema(tmp_path)
    kg = tmp_path / "kg.json"
    manifest, changelog = tmp_path / "m.json", tmp_path / "c.md"
    payload = {
        "source_files": [],
        "nodes": [
            {"id": "repo-a", "type": "Repo", "name": "a", "path": "t/a"},
            {"id": "tech-mongodb", "type": "TechStack",
             "label": "mongodb", "term_id": "sr:mongodb"},
            {"id": "tech-beanie", "type": "TechStack",
             "label": "beanie", "term_id": "sr:mongodb"},
        ],
        "edges": [
            {"type": "USES_TECH", "source": "repo-a", "target": "tech-beanie"},
        ],
    }
    ingest_entities(json.loads(json.dumps(payload)), kg, schema, manifest, changelog)

    cfg = {"merge": {"strategies": [
        {"type": "alias", "node_type": "TechStack", "id_prefix": "tech-"}
    ]}}
    data = json.loads(kg.read_text())
    merge_map, notes, props = build_merge_map(data, strategies_from_config(cfg, LocalVocabulary()))
    data = apply_merges(data, merge_map, notes, props)
    data.pop("_merge_stats", None)
    kg.write_text(json.dumps(data))
    assert {n["id"] for n in data["nodes"]} == {"repo-a", "tech-mongodb"}

    # Re-ingest the *same* pre-merge payload.
    _, stats = ingest_entities(
        json.loads(json.dumps(payload)), kg, schema, manifest, changelog
    )
    after = json.loads(kg.read_text())
    assert {n["id"] for n in after["nodes"]} == {"repo-a", "tech-mongodb"}
    assert stats["redirected"] == 1
    assert after["edges"][0]["target"] == "tech-mongodb"


def test_review_queue_does_not_re_ask_the_same_question(tmp_path):
    """A re-ingest doubled the queue from 40 to 80 identical items."""
    from epistemic_agent.review.emitters import emit_grounding_candidate
    from epistemic_agent.review.queue import ReviewQueue

    q = ReviewQueue(tmp_path / "review")

    def item():
        return emit_grounding_candidate(
            source_project="p", entity_label="foo", entity_id="tech-foo",
            candidates=[{"term_id": "x:foo", "label": "Foo"}], cluster_score=0.6,
        )

    assert q.append_unique(item()) is True
    assert q.append_unique(item()) is False
    assert q.pending_count() == 1


def test_apply_merges_asserts_vocabulary_labels_on_the_survivor():
    """Regression: a local `props = clone.setdefault(...)` shadowed the
    function's `props` parameter, so vocabulary labels were never applied and
    `tech-mongodb` kept displaying as `beanie`."""
    kg = {
        "nodes": [
            {"id": "tech-mongodb", "type": "TechStack",
             "properties": {"label": "mongodb", "term_id": "sr:mongodb"}},
            {"id": "tech-beanie", "type": "TechStack",
             "properties": {"label": "beanie", "term_id": "sr:mongodb"}},
            # Forces the clone path, which is where the shadowing happened.
            {"id": "tech-langgraph-checkpoint-sqlite", "type": "TechStack",
             "properties": {"label": "langgraph-checkpoint-sqlite",
                            "term_id": "af:langgraph"}},
        ],
        "edges": [],
    }
    cfg = {"merge": {"strategies": [
        {"type": "alias", "node_type": "TechStack", "id_prefix": "tech-"}
    ]}}
    merge, notes, props = build_merge_map(kg, strategies_from_config(cfg, LocalVocabulary()))
    out = apply_merges(kg, merge, notes, props)
    by_id = {n["id"]: n for n in out["nodes"]}
    assert by_id["tech-mongodb"]["properties"]["label"] == "MongoDB"
    assert by_id["tech-langgraph"]["properties"]["label"] == "LangGraph"
    assert out["_merge_stats"]["properties_asserted"] > 0


def test_multi_token_flag_leaves_grounded_nodes_alone():
    """Vocabulary labels legitimately contain separators ('Jira / Confluence').
    Quarantining a grounded node made MultiTokenFlag and AliasMerge rename it
    back and forth forever, so the merge never reached a fixed point."""
    kg = {
        "nodes": [
            {"id": "tech-atlassian", "type": "TechStack",
             "properties": {"label": "Jira / Confluence", "term_id": "si:atlassian"}},
            {"id": "tech-unresolved", "type": "TechStack",
             "properties": {"label": "thing-a/thing-b"}},
        ],
        "edges": [],
    }
    cfg = {"merge": {"strategies": [{"type": "multi_token", "node_type": "TechStack"}]}}
    merge, _, _ = build_merge_map(kg, strategies_from_config(cfg))
    assert "tech-atlassian" not in merge
    assert merge["tech-unresolved"] == "tech-unresolved-multi"


def test_merge_reaches_a_fixed_point():
    """One more round after applying must produce nothing."""
    kg = {
        "nodes": [
            {"id": "tech-atlassian", "type": "TechStack",
             "properties": {"label": "jira", "term_id": "si:atlassian"}},
            {"id": "tech-confluence", "type": "TechStack",
             "properties": {"label": "confluence", "term_id": "si:atlassian"}},
        ],
        "edges": [],
    }
    cfg = {"merge": {"strategies": [
        {"type": "alias", "node_type": "TechStack", "id_prefix": "tech-"},
        {"type": "multi_token", "node_type": "TechStack"},
    ]}}
    vocab = LocalVocabulary()
    m1, n1, p1 = build_merge_map(kg, strategies_from_config(cfg, vocab))
    kg = apply_merges(kg, m1, n1, p1)
    kg.pop("_merge_stats", None)
    m2, _, _ = build_merge_map(kg, strategies_from_config(cfg, vocab))
    assert m2 == {}, f"merge did not converge: {m2}"


# ---------------------------------------------------------------------------
# Inspect (capmap show)


def test_resolve_prefers_exact_id_and_path():
    from epistemic_agent.analysis.inspect import resolve_node

    idx = V.GraphIndex(_tiny_graph())
    assert resolve_node(idx, "repo-x--svc")[0] == ["repo-x--svc"]
    assert resolve_node(idx, "x/svc")[0] == ["repo-x--svc"]


def test_resolve_unions_vocabulary_and_substring():
    """A query that grounds to an internal system name but also substring-
    matches a repo should show both — showing both beats silently picking
    one."""
    from epistemic_agent.analysis.inspect import resolve_node

    kg = _tiny_graph()
    kg["nodes"] += [
        {"id": "sys-thing", "type": "InternalSystem",
         "properties": {"label": "Kubernetes", "term_id": "id:kubernetes"}},
        {"id": "repo-z--kubernetes-tool", "type": "Repo",
         "properties": {"name": "kubernetes-tool", "path": "z/kubernetes-tool"}},
    ]
    idx = V.GraphIndex(kg)
    hits, _ = resolve_node(idx, "kubernetes", LocalVocabulary())
    assert set(hits) == {"sys-thing", "repo-z--kubernetes-tool"}


def test_describe_node_groups_edges_and_teams():
    from epistemic_agent.analysis.inspect import describe_node

    idx = V.GraphIndex(_tiny_graph())
    info = describe_node(idx, "tech-a")
    assert info["type"] == "TechStack"
    assert info["degree"] == 2
    assert {r["edge_type"] for r in info["edges"]} == {"USES_TECH"}
    assert {t["team"] for t in info["team_breakdown"]} == {"x", "y"}


# ---------------------------------------------------------------------------
# Subgraph (GraphRAG retrieval primitive — not wired into CLI/agent yet)


def _chain_graph() -> dict:
    """A > B > C > D repo/team/domain chain, plus an isolated island, so BFS
    distance ordering and unreachable-target handling both have something to
    bite on."""
    return {
        "nodes": [
            {"id": "repo-a", "type": "Repo", "properties": {"path": "a/svc"}},
            {"id": "team-a", "type": "Team", "properties": {"name": "a"}},
            {"id": "tech-b", "type": "TechStack", "properties": {"label": "B"}},
            {"id": "domain-c", "type": "Domain", "properties": {"label": "C"}},
            {"id": "repo-island", "type": "Repo", "properties": {"path": "z/island"}},
        ],
        "edges": [
            {"id": "1", "type": "OWNED_BY", "source": "repo-a", "target": "team-a"},
            {"id": "2", "type": "USES_TECH", "source": "repo-a", "target": "tech-b"},
            {"id": "3", "type": "IN_DOMAIN", "source": "repo-a", "target": "domain-c"},
        ],
    }


def test_bounded_subgraph_stays_within_hops():
    from epistemic_agent.analysis.subgraph import bounded_subgraph

    idx = V.GraphIndex(_chain_graph())
    result = bounded_subgraph(idx, ["repo-a"], hops=1)
    ids = {n["id"] for n in result["nodes"]}
    assert ids == {"repo-a", "team-a", "tech-b", "domain-c"}
    assert not result["truncated"]
    assert "repo-island" not in ids


def test_bounded_subgraph_truncates_by_max_nodes_but_keeps_seeds():
    from epistemic_agent.analysis.subgraph import bounded_subgraph

    idx = V.GraphIndex(_chain_graph())
    result = bounded_subgraph(idx, ["repo-a"], hops=2, max_nodes=2)
    ids = {n["id"] for n in result["nodes"]}
    assert "repo-a" in ids
    assert len(ids) == 2
    assert result["truncated"]


def test_bounded_subgraph_ignores_unknown_seeds():
    from epistemic_agent.analysis.subgraph import bounded_subgraph

    idx = V.GraphIndex(_chain_graph())
    result = bounded_subgraph(idx, ["does-not-exist"], hops=2)
    assert result == {"nodes": [], "edges": [], "seeds": [], "truncated": False}


def test_path_between_finds_shortest_route():
    from epistemic_agent.analysis.subgraph import path_between

    idx = V.GraphIndex(_chain_graph())
    result = path_between(idx, "team-a", "domain-c", max_hops=4)
    assert result["found"]
    assert all(p[0] == "team-a" and p[-1] == "domain-c" for p in result["paths"])
    assert all(len(p) == 5 for p in result["paths"])  # team -> edge -> repo -> edge -> domain


def test_path_between_reports_unreachable_nodes():
    from epistemic_agent.analysis.subgraph import path_between

    idx = V.GraphIndex(_chain_graph())
    result = path_between(idx, "team-a", "repo-island", max_hops=4)
    assert result == {"paths": [], "found": False}


def test_path_between_same_node_is_a_trivial_path():
    from epistemic_agent.analysis.subgraph import path_between

    idx = V.GraphIndex(_chain_graph())
    result = path_between(idx, "repo-a", "repo-a")
    assert result == {"paths": [["repo-a"]], "found": True}


# ---------------------------------------------------------------------------
# Export


def _export_graph() -> dict:
    kg = _tiny_graph()
    # Values that break naive serialisers: quotes, angle brackets, ampersands,
    # a list, a bool, a number, and a non-ASCII label.
    kg["nodes"][0]["properties"].update(
        {
            "description": 'a "quoted" <tag> & ampersand',
            "archived": False,
            "star_count": 3,
            "_merged_from": ["old-1", "old-2"],
            "label": "服务 / Service",
        }
    )
    return kg


def test_graphml_is_wellformed_xml_and_escapes():
    import xml.etree.ElementTree as ET

    from epistemic_agent.export.formats import to_graphml

    root = ET.fromstring(to_graphml(_export_graph()))
    ns = root.tag.split("}")[0].strip("{")
    assert len(root.findall(f".//{{{ns}}}node")) == len(_export_graph()["nodes"])
    assert len(root.findall(f".//{{{ns}}}edge")) == len(_export_graph()["edges"])
    # Every declared key must exist, or Gephi silently drops the attribute.
    declared = {k.get("id") for k in root.findall(f".//{{{ns}}}key")}
    assert "label" in declared and "ntype" in declared


def test_gexf_is_wellformed_xml():
    import xml.etree.ElementTree as ET

    from epistemic_agent.export.formats import to_gexf

    root = ET.fromstring(to_gexf(_export_graph()))
    ns = root.tag.split("}")[0].strip("{")
    assert len(root.findall(f".//{{{ns}}}node")) == len(_export_graph()["nodes"])


def test_cypher_escapes_quotes_and_backticks_underscore_keys():
    from epistemic_agent.export.formats import to_cypher

    out = to_cypher(_export_graph())
    assert "CREATE CONSTRAINT" in out
    assert "\\'" in out or '"quoted"' in out          # the quoted string survived
    assert "`_merged_from`" in out                     # leading underscore backticked
    assert "archived: false" in out                    # bool, not the string "False"


def test_sources_dropped_by_default():
    from epistemic_agent.export.formats import to_graphml

    kg = _export_graph()
    kg["nodes"][0]["properties"]["_sources"] = "/very/long/path.json; /another.json"
    assert "_sources" not in to_graphml(kg)
    assert "_sources" in to_graphml(kg, keep_sources=True)


def test_dot_refuses_a_hairball():
    from epistemic_agent.export.formats import to_dot

    kg = {"nodes": [{"id": f"n{i}", "type": "Repo", "properties": {}} for i in range(400)],
          "edges": []}
    with pytest.raises(ValueError, match="超过上限"):
        to_dot(kg, max_nodes=300)
    assert to_dot(kg, max_nodes=500).startswith("digraph")


def test_html_viewer_is_self_contained_and_parseable():
    import re

    from epistemic_agent.export.viewer import build_html

    html = build_html(_export_graph(), title="t")
    assert "http://" not in html and "https://" not in html.split("<script>")[0].replace(
        "http://www.w3.org", ""
    )
    raw = re.search(r"^const DATA = (.*);$", html, re.M).group(1)
    data = json.loads(raw.replace("<\\/", "</"))
    assert set(data["views"]) == {"tech", "team", "domain", "full"}
    # An unescaped </script> inside the data would end the tag early.
    assert "</script>" not in raw


def test_viewer_views_are_derived_not_empty():
    from epistemic_agent.export.viewer import view_summary

    rows = {r["key"]: r for r in view_summary(_tiny_graph())}
    assert rows["full"]["nodes"] == len(_tiny_graph()["nodes"])
    assert rows["team"]["nodes"] > 0


# ---------------------------------------------------------------------------
# Viewer localisation


def _viewer_payload(**kw) -> dict:
    """The DATA blob the page actually runs on, parsed back out of the HTML."""
    import re

    from epistemic_agent.export.viewer import build_html

    html = build_html(_export_graph(), title="t", **kw)
    raw = re.search(r"^const DATA = (.*);$", html, re.M).group(1)
    return {"html": html, "data": json.loads(raw.replace("<\\/", "</"))}


def test_viewer_locales_have_the_same_keys():
    """A key present in one locale and missing in the other renders as
    `undefined` in the page — cheap to pin, invisible otherwise."""
    from epistemic_agent.export.viewer import UI_STRINGS, VIEW_BUILDERS, VIEW_META

    assert set(UI_STRINGS["zh"]) == set(UI_STRINGS["en"])
    for lang, meta in VIEW_META.items():
        assert set(meta) == set(VIEW_BUILDERS), lang


def test_viewer_english_page_carries_no_chinese_ui_text():
    """The template must not hold locale literals of its own. JS comments are
    exempt — they are code, not interface."""
    out = _viewer_payload(lang="en")
    assert '<html lang="en">' in out["html"]
    assert out["data"]["i18n"]["detailEmpty"] == "Click a node to see its details."
    assert out["data"]["views"]["team"]["name"] == "Team ↔ tech"

    body = out["html"].split("<script>")[0]
    assert not re.search(r"[一-鿿]", body), "Chinese left in the English markup"
    assert not re.search(r"[一-鿿]", json.dumps(out["data"]["i18n"], ensure_ascii=False))


def test_viewer_defaults_to_chinese_and_falls_back_for_unknown_langs():
    zh = _viewer_payload()
    assert '<html lang="zh">' in zh["html"]
    assert zh["data"]["i18n"]["detailEmpty"] == "点一个节点看详情。"
    assert _viewer_payload(lang="klingon")["data"]["i18n"] == zh["data"]["i18n"]


def test_view_summary_is_localised():
    from epistemic_agent.export.viewer import view_summary

    en = {r["key"]: r for r in view_summary(_tiny_graph(), lang="en")}
    zh = {r["key"]: r for r in view_summary(_tiny_graph())}
    assert en["full"]["name"] == "Everything"
    # Only the labels change; the graphs behind them are the same.
    assert [r["nodes"] for r in en.values()] == [r["nodes"] for r in zh.values()]


def test_run_export_passes_lang_through_to_the_page(tmp_path):
    from epistemic_agent.export.viewer import run_export

    result = run_export(_export_project(tmp_path), fmt="html", lang="en")
    assert '<html lang="en">' in result["output_path"].read_text()


def _export_project(tmp_path: Path) -> Project:
    kg_path = tmp_path / "kg" / "capability-map.json"
    kg_path.parent.mkdir(parents=True)
    kg_path.write_text(json.dumps(_tiny_graph()))
    return Project(tmp_path, {"name": "t"})


def test_run_export_html_writes_a_file_and_reports_counts(tmp_path):
    from epistemic_agent.export.viewer import run_export

    result = run_export(_export_project(tmp_path), fmt="html")
    assert result["nodes"] == len(_tiny_graph()["nodes"])
    assert result["output_path"] == tmp_path / "kg" / "capability-map.html"
    assert result["output_path"].exists()


def test_run_export_node_type_filter_drops_disconnected_edges(tmp_path):
    """Domain-only nodes share no edge with each other (they only connect to
    Repo) — the filter must still succeed and flag the empty edge set rather
    than silently producing a file with nodes but no connections."""
    from epistemic_agent.export.viewer import run_export

    result = run_export(_export_project(tmp_path), fmt="dot", node_type="Domain")
    assert any("no edges survived" in n for n in result["notes"])


def test_run_export_rejects_unknown_format(tmp_path):
    from epistemic_agent.export.viewer import run_export

    assert "error" in run_export(_export_project(tmp_path), fmt="pptx")


# ---------------------------------------------------------------------------
# Agent tools


def _agent_project(tmp_path: Path, merge_strategies: list | None = None) -> Project:
    """A minimal but complete instance directory for the agent tool tests —
    same shape `find_project()` would resolve, built by hand so tests don't
    depend on `capmap init`'s template."""
    schema = tmp_path / "schema.yaml"
    schema.write_text(
        "nodes:\n  Repo: {}\n  TechStack: {}\nedges:\n  USES_TECH: {}\n"
    )
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "orient-state.md").write_text("# Skill: Orient State\n")
    config = {
        "name": "agent-test",
        "paths": {"schema": "schema.yaml"},
        "merge": {"strategies": merge_strategies or []},
    }
    return Project(tmp_path, config)


def test_orient_state_follows_first_run_protocol_when_no_kg(tmp_path):
    """A brand-new instance has no KG yet — the tool must say so and tell the
    caller not to scaffold, not crash trying to load a missing file."""
    proj = _agent_project(tmp_path)
    out = _orient_state(proj)
    assert "first-run protocol" in out
    assert "Do NOT scaffold" in out


def test_orient_state_summarises_an_existing_graph(tmp_path):
    proj = _agent_project(tmp_path)
    payload = {
        "source_files": [],
        "nodes": [
            {"id": "repo-a", "type": "Repo", "name": "a", "path": "t/a"},
            {"id": "tech-langgraph", "type": "TechStack", "label": "LangGraph"},
        ],
        "edges": [{"type": "USES_TECH", "source": "repo-a", "target": "tech-langgraph"}],
    }
    ingest_entities(payload, proj.kg_path, proj.schema_path, proj.manifest_path, proj.changelog_path)
    out = _orient_state(proj)
    assert "2 nodes, 1 edges" in out
    assert "Pending review items: 0" in out


def test_read_skill_lists_available_skills_on_miss(tmp_path):
    proj = _agent_project(tmp_path)
    out = _read_skill(proj, "does-not-exist")
    assert "orient-state" in out
    assert "does-not-exist" in out


def test_read_skill_returns_file_contents_on_hit(tmp_path):
    proj = _agent_project(tmp_path)
    assert _read_skill(proj, "orient-state") == "# Skill: Orient State\n"


def test_ingest_payload_is_idempotent_and_force_overrides_it(tmp_path):
    """Mirrors `capmap ingest`'s own idempotence contract (manifest content
    hash) through the agent tool wrapper — a re-run with unchanged source
    files must not silently re-add nodes a merge folded away."""
    proj = _agent_project(tmp_path)
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(
        json.dumps(
            {
                "source_files": [str(payload_path)],
                "nodes": [{"id": "repo-a", "type": "Repo", "name": "a", "path": "t/a"}],
                "edges": [],
            }
        )
    )
    first = _ingest_payload(proj, str(payload_path))
    assert "Ingest: +1 nodes" in first

    second = _ingest_payload(proj, str(payload_path))
    assert "Nothing to do" in second

    forced = _ingest_payload(proj, str(payload_path), force=True)
    assert "Ingest:" in forced


def test_vocab_draft_requires_a_source(tmp_path):
    proj = _agent_project(tmp_path)
    assert "Pass a payload path" in _vocab_draft(proj)


def test_vocab_draft_writes_outside_vocabulary_dir(tmp_path):
    proj = _agent_project(tmp_path)
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": "tech-a", "type": "TechStack", "label": "AcmeBillingCore"},
                    {"id": "tech-b", "type": "TechStack", "label": "acme-billing-core"},
                ]
            }
        )
    )
    out = _vocab_draft(proj, payload=str(payload_path))
    assert "TechStack: 2 labels -> 1 groups" in out
    assert (tmp_path / "kg" / "vocabulary-draft" / "techstack.yaml").exists()
    assert not (tmp_path / "vocabulary").exists()


def test_merge_dry_run_reports_without_writing_the_kg(tmp_path):
    """dry_run must leave the on-disk KG untouched — only merge_apply may write."""
    proj = _agent_project(
        tmp_path, merge_strategies=[{"type": "alias", "node_type": "TechStack", "id_prefix": "tech-"}]
    )
    kg = {
        "nodes": [
            {"id": "tech-mongodb", "type": "TechStack",
             "properties": {"label": "mongodb", "term_id": "sr:mongodb"}},
            {"id": "tech-beanie", "type": "TechStack",
             "properties": {"label": "beanie", "term_id": "sr:mongodb"}},
        ],
        "edges": [],
    }
    proj.kg_path.parent.mkdir(parents=True, exist_ok=True)
    proj.kg_path.write_text(json.dumps(kg))

    dry = _merge_dry_run(proj)
    assert "DRY RUN" in dry
    assert "Merges: 1" in dry
    assert len(json.loads(proj.kg_path.read_text())["nodes"]) == 2

    applied = _merge_apply(proj)
    assert "DRY RUN" not in applied
    assert len(json.loads(proj.kg_path.read_text())["nodes"]) == 1


def test_view_experts_requires_a_query(tmp_path):
    proj = _agent_project(tmp_path)
    proj.kg_path.parent.mkdir(parents=True, exist_ok=True)
    proj.kg_path.write_text(json.dumps({"nodes": [], "edges": []}))
    assert "needs a query" in _view(proj, "experts")


def test_subgraph_follows_first_run_protocol_when_no_kg(tmp_path):
    proj = _agent_project(tmp_path)
    assert "first-run protocol" in _subgraph(proj, ["repo-a"])


def test_subgraph_requires_a_seed(tmp_path):
    proj = _agent_project(tmp_path)
    proj.kg_path.parent.mkdir(parents=True, exist_ok=True)
    proj.kg_path.write_text(json.dumps({"nodes": [], "edges": []}))
    assert "at least one seed" in _subgraph(proj, [])


def test_subgraph_reports_unresolved_seed(tmp_path):
    proj = _agent_project(tmp_path)
    proj.kg_path.parent.mkdir(parents=True, exist_ok=True)
    proj.kg_path.write_text(json.dumps(_chain_graph()))
    out = json.loads(_subgraph(proj, ["nothing-like-this-exists"]))
    assert out["error"] == "no seed resolved to a node"
    assert out["resolution"][0]["matched"] == []


def test_subgraph_returns_bounded_neighbourhood(tmp_path):
    proj = _agent_project(tmp_path)
    proj.kg_path.parent.mkdir(parents=True, exist_ok=True)
    proj.kg_path.write_text(json.dumps(_chain_graph()))
    out = json.loads(_subgraph(proj, ["repo-a"], hops=1))
    ids = {n["id"] for n in out["subgraph"]["nodes"]}
    assert ids == {"repo-a", "team-a", "tech-b", "domain-c"}
    assert out["resolution"][0]["matched"] == ["repo-a"]


def test_subgraph_find_path_needs_exactly_two_unambiguous_seeds(tmp_path):
    proj = _agent_project(tmp_path)
    proj.kg_path.parent.mkdir(parents=True, exist_ok=True)
    proj.kg_path.write_text(json.dumps(_chain_graph()))
    assert "needs exactly two seeds" in _subgraph(proj, ["repo-a"], find_path=True)


def test_subgraph_truncation_never_produces_invalid_json(tmp_path):
    """Regression: a blind `json.dumps(...)[:N]` string slice cuts some
    node's property value mid-string on any real-sized graph (long
    `description`/`_sources` text), handing the model invalid JSON. The fix
    drops whole nodes and caps individual field lengths instead — this must
    stay parseable no matter how big the neighbourhood is."""
    proj = _agent_project(tmp_path)
    proj.kg_path.parent.mkdir(parents=True, exist_ok=True)
    big_kg = {
        "nodes": [
            {"id": "hub", "type": "Repo", "properties": {"path": "x/hub"}},
            *[
                {
                    "id": f"leaf-{i}",
                    "type": "Repo",
                    "properties": {
                        "path": f"x/leaf-{i}",
                        "description": "详细描述 " * 200,
                        "_sources": "/very/long/path/to/a/source/file.json, " * 20,
                    },
                }
                for i in range(80)
            ],
        ],
        "edges": [
            {"id": f"e{i}", "type": "USES_TECH", "source": "hub", "target": f"leaf-{i}"}
            for i in range(80)
        ],
    }
    proj.kg_path.write_text(json.dumps(big_kg))

    out = _subgraph(proj, ["hub"], hops=1, max_nodes=80)
    data = json.loads(out)  # raises if truncation broke the JSON
    assert data["subgraph"]["truncated"]
    assert len(data["subgraph"]["nodes"]) < 80
    for n in data["subgraph"]["nodes"]:
        for v in n["properties"].values():
            assert len(str(v)) <= 161  # 160 chars + the "…" marker


def test_subgraph_find_path_returns_a_path(tmp_path):
    proj = _agent_project(tmp_path)
    proj.kg_path.parent.mkdir(parents=True, exist_ok=True)
    proj.kg_path.write_text(json.dumps(_chain_graph()))
    out = json.loads(_subgraph(proj, ["team-a", "domain-c"], find_path=True))
    assert out["path"]["found"]
    assert out["path"]["paths"][0][0] == "team-a"
    assert out["path"]["paths"][0][-1] == "domain-c"


def test_export_follows_first_run_protocol_when_no_kg(tmp_path):
    proj = _agent_project(tmp_path)
    assert "first-run protocol" in _export(proj)


def test_export_writes_html_by_default(tmp_path):
    proj = _agent_project(tmp_path)
    proj.kg_path.parent.mkdir(parents=True, exist_ok=True)
    proj.kg_path.write_text(json.dumps(_tiny_graph()))
    out = _export(proj)
    assert "Wrote" in out
    assert (proj.root / "kg" / "capability-map.html").exists()


def test_review_status_is_read_only(tmp_path):
    from epistemic_agent.review.emitters import emit_grounding_candidate
    from epistemic_agent.review.queue import ReviewQueue

    proj = _agent_project(tmp_path)
    q = ReviewQueue(proj.review_dir)
    q.append_unique(
        emit_grounding_candidate(
            source_project="p", entity_label="foo", entity_id="tech-foo",
            candidates=[{"term_id": "x:foo", "label": "Foo"}], cluster_score=0.6,
        )
    )
    out = _review_status(proj)
    assert "1 pending review items" in out
    assert "vocabulary_grounding" in out
    assert "Read-only" in out


_TOOL_WHITELIST = {
    "orient_state",
    "read_skill",
    "run_bootstrap",
    "ingest_payload",
    "vocab_draft",
    "merge_dry_run",
    "merge_apply",
    "view",
    "subgraph",
    "review_status",
    "read_file",
    "fetch_url",
    "write_file",
    "export",
}


def test_build_openai_tools_never_exposes_a_vocabulary_write_or_review_resolution_tool(tmp_path):
    """Pins the agent's tool-set guardrail: exactly these 14 names, no more.
    `write_file` exists (unlike the old all-or-nothing guardrail) but must
    refuse `vocabulary/*.yaml`, `kg/`, and `review/` itself — see the
    `test_write_file_refuses_*` tests below for that half of the guarantee.
    If a future tool is added under a name outside this list, this test fails
    and forces a deliberate look before it ships."""
    proj = _agent_project(tmp_path)
    specs, dispatch = build_openai_tools(proj)
    assert {s["name"] for s in specs} == _TOOL_WHITELIST
    assert set(dispatch) == _TOOL_WHITELIST


def test_build_openai_tools_dispatch_maps_arguments_correctly(tmp_path):
    """Each dispatch entry must forward its parsed-JSON arguments to the right
    keyword on the underlying `_snake_case` function — a mismatch here would
    silently ignore an argument the model actually sent."""
    proj = _agent_project(tmp_path)
    proj.kg_path.parent.mkdir(parents=True, exist_ok=True)
    proj.kg_path.write_text(json.dumps({"nodes": [], "edges": []}))

    _, dispatch = build_openai_tools(proj)

    assert dispatch["read_skill"](name="orient-state") == "# Skill: Orient State\n"
    assert "no-such-skill" in dispatch["read_skill"](name="no-such-skill")
    assert "needs a query" in dispatch["view"](name="experts")
    assert dispatch["orient_state"]() == _orient_state(proj)
    assert dispatch["review_status"]() == _review_status(proj)
    assert dispatch["subgraph"](seeds=[]) == _subgraph(proj, [])


# ---------------------------------------------------------------------------
# read_file / fetch_url / write_file


def test_read_file_reads_and_reports_missing(tmp_path):
    f = tmp_path / "seed.yaml"
    f.write_text("repos: []\n")
    assert _read_file(str(f)) == "repos: []\n"
    assert "No file at" in _read_file(str(tmp_path / "does-not-exist.yaml"))


def test_read_file_truncates_large_files(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("x" * 100)
    out = _read_file(str(f), max_chars=10)
    assert out.startswith("x" * 10)
    assert "truncated, 100 chars total" in out


def test_fetch_url_returns_real_content_from_a_local_server():
    """Exercises the success path against a real HTTP response, not just the
    error branch — a local server keeps this independent of outside network."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'{"hello": "world"}')

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        out = _fetch_url(f"http://127.0.0.1:{server.server_port}/")
        assert out == '{"hello": "world"}'
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_fetch_url_reports_failure_instead_of_raising():
    out = _fetch_url("http://127.0.0.1:1/")  # port 1 refuses immediately
    assert "Fetch failed" in out


def test_write_file_accepts_the_three_setup_files_and_data_raw(tmp_path):
    proj = _agent_project(tmp_path)
    for rel, content in [
        ("config/project.yaml", "name: x\n"),
        ("schema/kg-schema.yaml", "nodes: {}\n"),
        ("scripts/bootstrap.py", "print('hi')\n"),
        ("data/raw/inventory.json", '{"a": 1}'),
    ]:
        msg = _write_file(proj, rel, content)
        assert "Wrote" in msg
        assert (proj.root / rel).read_text() == content


@pytest.mark.parametrize(
    "path",
    [
        "vocabulary/internal-system.yaml",
        "kg/capability-map.json",
        "review/items.jsonl",
        "CLAUDE.md",
        "../outside.txt",
        "data/raw/../../outside.txt",
    ],
)
def test_write_file_refuses_paths_outside_the_allowlist(tmp_path, path):
    """Pins the other half of the write_file guardrail: these paths must be
    refused and must not exist afterwards, regardless of how the traversal is
    spelled."""
    proj = _agent_project(tmp_path)
    msg = _write_file(proj, path, "anything")
    assert "Refused" in msg
    assert not (proj.root / path).exists()


def test_write_file_refuses_an_absolute_path(tmp_path):
    proj = _agent_project(tmp_path)
    target = tmp_path.parent / "escaped.txt"
    msg = _write_file(proj, str(target), "anything")
    assert "Refused" in msg
    assert not target.exists()


def test_write_file_refuses_invalid_yaml(tmp_path):
    proj = _agent_project(tmp_path)
    msg = _write_file(proj, "config/project.yaml", "name: [unterminated\n")
    assert "Refused" in msg
    assert "valid YAML" in msg


# ---------------------------------------------------------------------------
# .env.llm loading


def test_load_env_file_sets_undefined_vars(tmp_path, monkeypatch):
    from epistemic_agent.agent.runtime import _load_env_file

    monkeypatch.delenv("OPENAI_TEST_KEY", raising=False)
    (tmp_path / ".env.llm").write_text(
        "# a comment\n\nOPENAI_TEST_KEY=\"from-file\"\nMALFORMED LINE\n"
    )
    _load_env_file(tmp_path)
    assert os.environ["OPENAI_TEST_KEY"] == "from-file"


@pytest.mark.parametrize(
    "raw,expected",
    [
        # The bug: a copy-pasted example line's trailing comment used to
        # become part of the value, e.g. a model ID glued to "# no default
        # — deployment-specific".
        ("us.anthropic.claude-opus-5   # no default — deployment-specific", "us.anthropic.claude-opus-5"),
        ('"quoted#value"', "quoted#value"),  # '#' inside quotes is literal
        ("https://example.com/v1#frag   # trailing comment", "https://example.com/v1#frag"),
        ("plain-value", "plain-value"),
        ("#just a comment", ""),
    ],
)
def test_strip_env_value_removes_inline_comments(raw, expected):
    from epistemic_agent.agent.runtime import _strip_env_value

    assert _strip_env_value(raw) == expected


def test_load_env_file_strips_inline_comment_on_the_model_id(tmp_path, monkeypatch):
    """Regression: OPENAI_MODEL copy-pasted from .env.llm.example with its
    trailing comment intact used to print
    'via OpenAI-compatible endpoint (MODEL   # no default — deployment-specific).'
    instead of a clean model name."""
    from epistemic_agent.agent.runtime import _load_env_file

    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    (tmp_path / ".env.llm").write_text(
        "OPENAI_MODEL=us.anthropic.claude-opus-5   # no default — deployment-specific\n"
    )
    _load_env_file(tmp_path)
    assert os.environ["OPENAI_MODEL"] == "us.anthropic.claude-opus-5"


def test_load_env_file_never_overrides_an_already_exported_var(tmp_path, monkeypatch):
    """A real shell export must win over the file — the file is a fallback,
    not a way to silently override what the user already set."""
    from epistemic_agent.agent.runtime import _load_env_file

    monkeypatch.setenv("OPENAI_TEST_KEY", "from-shell")
    (tmp_path / ".env.llm").write_text("OPENAI_TEST_KEY=from-file\n")
    _load_env_file(tmp_path)
    assert os.environ["OPENAI_TEST_KEY"] == "from-shell"


def test_load_env_file_closest_directory_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_TEST_KEY", raising=False)
    from epistemic_agent.agent.runtime import _load_env_file

    (tmp_path / ".env.llm").write_text("OPENAI_TEST_KEY=root\n")
    nested = tmp_path / "instances" / "your-team"
    nested.mkdir(parents=True)
    (nested / ".env.llm").write_text("OPENAI_TEST_KEY=instance\n")
    _load_env_file(nested)
    assert os.environ["OPENAI_TEST_KEY"] == "instance"
