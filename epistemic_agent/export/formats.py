"""Serialise the graph into formats other tools already understand.

Rather than build layout algorithms, community detection and centrality
measures, hand the graph to tools that have had them for fifteen years:

* **GraphML / GEXF** → Gephi, Cytoscape, yEd, networkx
* **Cypher** → Neo4j, for people who want a real graph query language
* **DOT** → Graphviz, for a quick deterministic picture of a small subgraph

One shared decision: `_sources` is dropped by default. It holds a
semicolon-joined list of absolute file paths, which is essential provenance in
the KG and pure noise in a layout tool — it would show up in every Gephi
tooltip and inflate the file several times over. Pass `keep_sources=True` if
you are exporting for archival rather than for looking at.
"""
from __future__ import annotations

from typing import Any, Iterable
from xml.sax.saxutils import escape, quoteattr

# Dropped unless keep_sources: long, low-signal in a visualisation tool.
_BULKY_PROPS = {"_sources"}


def _clean_props(props: dict, keep_sources: bool) -> dict:
    out = {}
    for k, v in (props or {}).items():
        if not keep_sources and k in _BULKY_PROPS:
            continue
        if v is None or v == "" or v == [] or v == {}:
            continue
        out[k] = v
    return out


def _flatten(value: Any) -> Any:
    """GraphML/GEXF attributes are scalars. Lists become joined strings."""
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}={v}" for k, v in value.items())
    return value


def _attr_type(values: Iterable[Any]) -> str:
    """Infer one type for an attribute from every value it takes."""
    seen = set()
    for v in values:
        if isinstance(v, bool):
            seen.add("boolean")
        elif isinstance(v, int):
            seen.add("int")
        elif isinstance(v, float):
            seen.add("double")
        else:
            seen.add("string")
    if seen == {"boolean"}:
        return "boolean"
    if seen == {"int"}:
        return "int"
    if seen <= {"int", "double"}:
        return "double"
    return "string"


def _collect_schema(items: list[dict], keep_sources: bool) -> dict[str, str]:
    """attribute name -> declared type, across every item."""
    values: dict[str, list[Any]] = {}
    for it in items:
        for k, v in _clean_props(it.get("properties") or {}, keep_sources).items():
            values.setdefault(k, []).append(_flatten(v))
    return {k: _attr_type(vs) for k, vs in sorted(values.items())}


def _fmt(value: Any, declared: str) -> str:
    v = _flatten(value)
    if declared == "boolean":
        return "true" if v else "false"
    return str(v)


# ---------------------------------------------------------------------------
# GraphML


def to_graphml(kg: dict, keep_sources: bool = False) -> str:
    nodes = kg.get("nodes", [])
    edges = kg.get("edges", [])
    node_schema = _collect_schema(nodes, keep_sources)
    edge_schema = _collect_schema(edges, keep_sources)

    L = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns"',
        '         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '         xsi:schemaLocation="http://graphml.graphdrawing.org/xmlns'
        ' http://graphml.graphdrawing.org/xmlns/1.0/graphml.xsd">',
        # `label` and `type` are declared first so Gephi picks them up as the
        # display label and the partition attribute without configuration.
        '  <key id="label" for="node" attr.name="label" attr.type="string"/>',
        '  <key id="ntype" for="node" attr.name="type" attr.type="string"/>',
        '  <key id="etype" for="edge" attr.name="type" attr.type="string"/>',
        '  <key id="weight" for="edge" attr.name="weight" attr.type="double"/>',
    ]
    for k, t in node_schema.items():
        if k == "label":
            continue
        L.append(f'  <key id="n_{escape(k)}" for="node" attr.name={quoteattr(k)} '
                 f'attr.type="{t}"/>')
    for k, t in edge_schema.items():
        L.append(f'  <key id="e_{escape(k)}" for="edge" attr.name={quoteattr(k)} '
                 f'attr.type="{t}"/>')

    L.append('  <graph id="capability-map" edgedefault="directed">')
    for n in nodes:
        props = _clean_props(n.get("properties") or {}, keep_sources)
        label = props.get("label") or props.get("name") or n["id"]
        L.append(f'    <node id={quoteattr(n["id"])}>')
        L.append(f'      <data key="label">{escape(str(label))}</data>')
        L.append(f'      <data key="ntype">{escape(n["type"])}</data>')
        for k, v in props.items():
            if k == "label":
                continue
            L.append(f'      <data key="n_{escape(k)}">'
                     f'{escape(_fmt(v, node_schema[k]))}</data>')
        L.append("    </node>")
    for e in edges:
        props = _clean_props(e.get("properties") or {}, keep_sources)
        L.append(f'    <edge id={quoteattr(e["id"])} source={quoteattr(e["source"])} '
                 f'target={quoteattr(e["target"])}>')
        L.append(f'      <data key="etype">{escape(e["type"])}</data>')
        L.append('      <data key="weight">1.0</data>')
        for k, v in props.items():
            L.append(f'      <data key="e_{escape(k)}">'
                     f'{escape(_fmt(v, edge_schema[k]))}</data>')
        L.append("    </edge>")
    L += ["  </graph>", "</graphml>", ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# GEXF


def to_gexf(kg: dict, keep_sources: bool = False) -> str:
    nodes = kg.get("nodes", [])
    edges = kg.get("edges", [])
    node_schema = _collect_schema(nodes, keep_sources)
    node_schema.setdefault("type", "string")
    idx = {k: str(i) for i, k in enumerate(sorted(node_schema))}

    L = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gexf xmlns="http://gexf.net/1.3" version="1.3">',
        "  <meta>",
        "    <creator>epistemic-agent</creator>",
        "    <description>technology capability map</description>",
        "  </meta>",
        '  <graph mode="static" defaultedgetype="directed">',
        '    <attributes class="node">',
    ]
    for k in sorted(node_schema):
        L.append(f'      <attribute id="{idx[k]}" title={quoteattr(k)} '
                 f'type="{node_schema[k]}"/>')
    L += ["    </attributes>", "    <nodes>"]
    for n in nodes:
        props = _clean_props(n.get("properties") or {}, keep_sources)
        props["type"] = n["type"]
        label = props.get("label") or props.get("name") or n["id"]
        L.append(f'      <node id={quoteattr(n["id"])} label={quoteattr(str(label))}>')
        L.append("        <attvalues>")
        for k, v in props.items():
            if k in idx:
                L.append(f'          <attvalue for="{idx[k]}" '
                         f'value={quoteattr(_fmt(v, node_schema[k]))}/>')
        L += ["        </attvalues>", "      </node>"]
    L += ["    </nodes>", "    <edges>"]
    for i, e in enumerate(edges):
        L.append(f'      <edge id="{i}" source={quoteattr(e["source"])} '
                 f'target={quoteattr(e["target"])} label={quoteattr(e["type"])}/>')
    L += ["    </edges>", "  </graph>", "</gexf>", ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Cypher (Neo4j)


def _cypher_value(v: Any) -> str:
    v = _flatten(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ") + "'"


def _cypher_key(k: str) -> str:
    """Neo4j property keys starting with `_` need backticks."""
    return f"`{k}`" if not k[:1].isalpha() else k


def to_cypher(kg: dict, keep_sources: bool = False, batch: int = 500) -> str:
    nodes = kg.get("nodes", [])
    edges = kg.get("edges", [])
    L = [
        "// epistemic-agent → Neo4j",
        "// 用法：cat graph.cypher | cypher-shell -u neo4j -p <password>",
        "//",
        "// 先建约束，否则 344 个 Repo 的 MATCH 会做全表扫描。",
    ]
    for t in sorted({n["type"] for n in nodes}):
        L.append(f"CREATE CONSTRAINT {t.lower()}_id IF NOT EXISTS "
                 f"FOR (n:{t}) REQUIRE n.id IS UNIQUE;")
    L.append("")

    for i, n in enumerate(nodes):
        if i % batch == 0:
            L.append(f"// --- nodes {i}–{min(i + batch, len(nodes)) - 1} ---")
        props = _clean_props(n.get("properties") or {}, keep_sources)
        pairs = [f"id: {_cypher_value(n['id'])}"]
        pairs += [f"{_cypher_key(k)}: {_cypher_value(v)}" for k, v in props.items()]
        L.append(f"CREATE (:{n['type']} {{{', '.join(pairs)}}});")
    L.append("")

    for i, e in enumerate(edges):
        if i % batch == 0:
            L.append(f"// --- edges {i}–{min(i + batch, len(edges)) - 1} ---")
        L.append(
            f"MATCH (a {{id: {_cypher_value(e['source'])}}}), "
            f"(b {{id: {_cypher_value(e['target'])}}}) "
            f"CREATE (a)-[:{e['type']}]->(b);"
        )
    L += ["", "// 试试这些查询：", "//   MATCH (r:Repo)-[:USES_TECH]->(t:TechStack) "
          "RETURN t.label, count(r) ORDER BY count(r) DESC LIMIT 20;",
          "//   MATCH (a:Repo)-[:USES_TECH]->(t)<-[:USES_TECH]-(b:Repo) "
          "WHERE a.id < b.id RETURN a.name, b.name, count(t) ORDER BY count(t) DESC LIMIT 20;",
          ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# DOT (Graphviz)


_DOT_COLORS = {
    "Repo": "#4C8BF5",
    "Team": "#34A853",
    "TechStack": "#FBBC05",
    "Capability": "#EA4335",
    "Pattern": "#A142F4",
    "Domain": "#00ACC1",
    "InternalSystem": "#F4511E",
    "Person": "#8D6E63",
    "Untyped": "#9E9E9E",
}


def to_dot(kg: dict, max_nodes: int = 300) -> str:
    """Graphviz DOT. Refuses to draw a hairball — DOT has no interactive
    filtering, so a 929-node picture is unreadable. Filter before exporting."""
    nodes = kg.get("nodes", [])
    if len(nodes) > max_nodes:
        raise ValueError(
            f"DOT 导出被拒绝：{len(nodes)} 个节点超过上限 {max_nodes}。"
            "Graphviz 画不出可读的大图，先用 --node-type / --domain 过滤，"
            "或改用 --format html / graphml。"
        )
    keep = {n["id"] for n in nodes}
    L = [
        "digraph capability_map {",
        '  graph [rankdir=LR, overlap=false, splines=true, bgcolor="white"];',
        '  node  [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=10];',
        '  edge  [fontname="Helvetica", fontsize=8, color="#90A4AE"];',
    ]
    for n in nodes:
        props = n.get("properties") or {}
        label = props.get("label") or props.get("name") or n["id"]
        colour = _DOT_COLORS.get(n["type"], "#CFD8DC")
        L.append(f'  {_dot_id(n["id"])} [label={_dot_str(label)}, '
                 f'fillcolor="{colour}", tooltip={_dot_str(n["type"])}];')
    for e in kg.get("edges", []):
        if e["source"] in keep and e["target"] in keep:
            L.append(f'  {_dot_id(e["source"])} -> {_dot_id(e["target"])} '
                     f'[label={_dot_str(e["type"])}];')
    L += ["}", ""]
    return "\n".join(L)


def _dot_id(s: str) -> str:
    return '"' + s.replace('"', '\\"') + '"'


def _dot_str(s: Any) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


# ---------------------------------------------------------------------------

WRITERS = {
    "graphml": to_graphml,
    "gexf": to_gexf,
    "cypher": to_cypher,
    "dot": to_dot,
}
