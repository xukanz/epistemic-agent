"""Smoke tests over the pieces that were actually wrong at some point.

Each of these encodes a bug found while bootstrapping the first instance, so
they are regression tests rather than coverage decoration.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from epistemic_agent.analysis import views as V
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
