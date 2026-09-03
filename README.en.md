**English** · [中文](README.md)

# epistemic-agent

An internal technical capability map — an AI agent that builds and continuously maintains an
organisation-level capability graph, to answer four questions management actually asks:

1. **What technical capabilities does this organisation actually have?**
2. **Which teams are reinventing the same wheel?**
3. **Which directions has nobody in the company covered?**
4. **Who do I talk to if I want to do X?**

The core idea is to fold information scattered across hundreds of repositories (dependency
manifests, CI configuration, repo metadata) into one structured knowledge graph: repos, teams,
tech stacks, business domains and internal systems as nodes; who-uses-what and who-owns-what as
edges. Once the graph exists, "who knows what" and "is anyone building this twice" become
queries over a graph, instead of a repo-by-repo trawl.

---

## Status: it runs, on real data

The framework has been validated against a real internal engineering organisation: one GitLab
scan of roughly 350 repositories and roughly 200 teams, the whole chain (bootstrap → ingest →
merge → health → view), producing:

```
~600 raw dependency tokens  →  ~250 canonical technology nodes after
                               normalisation, grounding and merging
~350 repos · ~200 teams · 10 business domains · ~25 internal systems
final graph: ~900 nodes / ~2000 edges
```

The kinds of signal it produces:

| Signal                              | What it means                                                                                   |
| ----------------------------------- | ----------------------------------------------------------------------------------------------- |
| bus factor = 1                      | technology held by exactly one team — it found hundreds                                         |
| suspected duplicate effort          | the `duplicates` view filters out fork/copy noise, leaving genuine cross-team, same-domain, heavily-overlapping candidates |
| capability gaps                     | in the vocabulary, absent from the graph                                                        |
| internal-system dependency concentration | a handful of internal gateways/platforms that most repos depend on                         |
| pending human review                | grounding-confidence entries in the 0.50–0.70 band, handed to a person to decide                |

The data used for that validation is one organisation's real internal repo listing and is not
distributed with this repository. What ships here is a small worked example that runs end to end:

```
cd instances/acme-corp
python scripts/bootstrap.py --out payload.bootstrap.json
capmap ingest payload.bootstrap.json
capmap merge && capmap health
capmap view duplicates
```

`instances/acme-corp/` is a fictional company ("Acme Corp", ~25 repos, 10 teams). Every value in
it is made up, and it runs exactly the same pipeline as the real validation did. A few signals are
planted deliberately in `data/seed.yaml` so they show up in the corresponding views: a pair of
cross-team, same-domain, heavily-overlapping independent duplicate efforts (the `independent`
class in the `duplicates` view), a pair of same-name forks/copies (the `same-name` class), several
single-team technologies (bus-factor=1 in the `risk` view), and one fictional internal gateway that
most repos depend on (internal-system concentration in the `risk` view). Running `capmap init` from
[Quick start](#quick-start) repeats the same pipeline on your own data.

- Look at the graph online, no commands to run: **[https://xukanz.github.io/epistemic-agent/index.en.html](https://xukanz.github.io/epistemic-agent/index.en.html)**
  (the output of `capmap export --format html --lang en`; an interactive force-directed graph
  with 4 built-in views. [Chinese version](https://xukanz.github.io/epistemic-agent/))
- What each command actually prints, pasted verbatim, step by step:
  [`instances/acme-corp/README.en.md`](instances/acme-corp/README.en.md)

---

## Quick start

```bash
uv venv .venv && uv pip install --python .venv -e ".[all]"

# Get capmap onto PATH — pick one:
source .venv/bin/activate                                    # this shell only
ln -sf "$PWD/.venv/bin/capmap" ~/.local/bin/capmap           # everywhere
# or just spell out .venv/bin/capmap every time

# Create a new instance
capmap init my-org
cd my-org && claude
```

After `capmap init`, open Claude Code and the agent reads `CLAUDE.md` and walks the first-contact
conversation: first it establishes who this map is for and what it has to answer, **then it asks
the two hard prerequisites** (where the vocabulary comes from, who reviews the queue), and only
then proposes a draft schema.

**Don't want to open Claude Code?** `capmap agent` is a standalone implementation of the same
first-contact flow — no Claude Code dependency, talks to any OpenAI-compatible endpoint. See
[Conversational operation](#conversational-operation-capmap-agent) below.

**Which files do you have to hand-write for a new instance?** Strictly speaking only
`config/project.yaml` — the framework uses that file to decide "is this an instance" (`find_project()`
walks up from the current directory looking for it). But you don't have to write it yourself:
`capmap init` generates the whole skeleton (`config/project.yaml`, `CLAUDE.md`,
`schema/kg-schema.yaml`, `vocabulary/`, `skills/*.md`, `scripts/bootstrap.py`, `data/raw/`), and
`purpose`, the schema trim and `bootstrap.py` are all written by `capmap agent` / Claude Code
during the first conversation, read back to you for confirmation before being written to disk.

**The raw data (everything under `data/raw/`) you must supply yourself** — it is the one thing the
agent will not generate for you. That directory describes *how to parse data*, not the data itself,
and CLAUDE.md explicitly forbids guessing URLs and API endpoints. How you hand it over is up to
you: drop existing CSV/YAML/JSON exports straight into `data/raw/`; if all you have is an internal
link, give the agent the link and it will read it with `fetch_url`; if the list only exists in your
head, say it out loud and it will write it into a file for you.

Once `scripts/bootstrap.py` and `vocabulary/` are in place, the daily loop is:

```bash
python scripts/bootstrap.py --out payload.bootstrap.json   # deterministic cold start, no LLM
capmap ingest payload.bootstrap.json                       # the only write path: grounding + 3-band routing + audit
capmap merge                                               # deduplicate per the strategies in config
capmap health                                              # quality signals + capability-map signals
capmap view coverage                                       # five analysis views
capmap view duplicates
capmap view gaps
capmap view experts -q "<some technology>"
capmap view risk

capmap show <node ID or tech name>                         # drill into a single node
capmap export -f html                                      # self-contained HTML viewer
capmap export -f html --lang en                            # …in English
capmap export -f graphml                                   # drag into Gephi / networkx
capmap export -f dot --around "<some repo>" --hops 1       # neighbourhood subgraph for Graphviz
```

---

## Conversational operation: capmap agent

You don't have to open Claude Code after `capmap init`. `capmap agent` is a standalone
conversation loop that reads the same instance `CLAUDE.md` and calls the 14 tools below itself,
instead of waiting for someone to type commands into a terminal:

```bash
capmap agent   # any OpenAI-compatible endpoint — OpenAI / Portkey / LiteLLM / Azure OpenAI / Claude, etc.
```

The 14 tools: `orient_state` / `read_skill` / `run_bootstrap` / `ingest_payload` / `vocab_draft` /
`merge_dry_run` / `merge_apply` / `view` / `subgraph` / `review_status` / `read_file` / `fetch_url` /
`write_file` / `export`. `subgraph` is the open-ended retrieval primitive alongside the 5 fixed
views — give it one or more nodes and it returns the neighbourhood subgraph or the shortest path
between two nodes, for the agent to phrase an answer from on the next turn; it draws no conclusions
itself. The guardrails use two deliberately different mechanisms:

- **Absent** — there is no tool for writing the vocabulary, resolving review-queue items, or
  asserting `DUPLICATES` edges. It physically cannot call them; this does not rely on good behaviour.
- **Constrained** — `write_file` is a real write tool, but the code hard-codes an allowlist of
  `config/project.yaml` / `schema/kg-schema.yaml` / `scripts/bootstrap.py` / `data/raw/*`.
  `vocabulary/`, `kg/` and `review/` are always refused, however the request is phrased.

Onboarding a new team needs no hand-written files: tell it which file or link the data is in, and
it will `read_file` / `fetch_url` the real data, draft `config/project.yaml` and
`schema/kg-schema.yaml` (reading the contents back to you before writing), and where custom parsing
code is needed, write and run `scripts/bootstrap.py` following `skills/bootstrap-instance.md`.

**Talking to it looks roughly like this:**

| You say | What it does |
|---|---|
| "Our repo listing is in `data/raw/repos.yaml`, get our capability map going" | First contact: `read_file` the data → ask who maintains the vocabulary and who reviews the queue → write `config/project.yaml` / `schema/kg-schema.yaml` (read back for confirmation) → `run_bootstrap` → `ingest_payload` |
| "What's the state of the graph right now" | `orient_state` + `review_status`, plus a few `view` calls if needed — the more specific the question, the fewer tools it calls and the faster it answers |
| "Any new data to process? Process it" | Checks `data/raw/` for un-ingested files, then `run_bootstrap` → `ingest_payload` |
| "Export an HTML graph for me" | `export` — only ever produces a derived file, never touches `kg/capability-map.json` |
| "Add this internal system to the vocabulary" / "These two teams are duplicating work, mark it" | Says plainly that it can't — there is no tool for vocabulary writes or `DUPLICATES` edges; it will point you at `capmap vocab-draft` / `capmap review` |

You need to set `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` (the model ID follows your
endpoint's own naming; there is no default). If your endpoint wants the key on a dedicated header
rather than the standard `Authorization: Bearer` (Portkey's `x-portkey-api-key`, for instance),
set `OPENAI_EXTRA_HEADER` as well. It speaks the standard OpenAI `/chat/completions` protocol, so
Anthropic's own beta OpenAI-compatible endpoint works too (`OPENAI_BASE_URL=https://api.anthropic.com/v1`,
with a Claude model ID) — but that is a migration subset, and Claude-native capabilities such as
extended thinking are not exposed through it.

---

## Key design decisions

**Why the synthesis layer is five deterministic views rather than a narrative document.** When
management asks "who knows what, is anyone building this twice", they want a table they can query,
filter and export — not a report to read cover to cover. So the analysis layer is five
deterministic views: `coverage` / `duplicates` / `gaps` / `experts` / `risk`. Each one is a fixed
query over the graph, with no LLM in the loop.

**Why the vocabulary layer is not a separate MCP service.** There is no ontology for technology
stacks with the size, external authority and cross-project value of EDAM / GO / MONDO. The
vocabulary here is a few hundred entries, specific to one organisation, and has to be maintained by
the same people who maintain the graph — standing up a second process for a handful of YAML files
buys nothing. The interface seam is kept open (`resolve_backend` and `McpVocabulary` in
`onto/client.py`), so an organisation with a shared internal technology taxonomy can swap the
backend without touching ingestion.

**`DUPLICATES` edges can only come from a human decision.** Two repos sharing most of a tech stack
does not mean they are doing the same thing — the `duplicates` view only produces candidates, and
they must go through the review queue before being written into the graph. `capmap agent` has no
tool that can produce this edge, exactly matching the boundary a human faces on the CLI.

---

## Directory layout

```
epistemic_agent/
  kg/store.py              plain-JSON graph storage
  ingest/document.py       the only write path: grounding + 3-band routing + manifest idempotency + changelog
  merge/
    canonical.py           normalisation: version stripping, multi-value splitting, path injectivity, noise detection
    strategies.py          AliasMerge / FuzzyLabelMerge / NaturalKeyGuard / MultiTokenFlag
  onto/
    client.py              find_terms / find_concepts / 3-band routing + MCP backend seam
    draft.py                vocabulary draft clustering (capmap vocab-draft)
    shards/*.yaml          12 general technology-stack shards, 219 terms
  bootstrap/generic.py     config-driven generic cold start; flat data sources need no code
  health/manifest.py       generic quality signals + four capability-map signals
  analysis/
    views.py               coverage / duplicates / gaps / experts / risk
    inspect.py             capmap show: single-node drill-down
    subgraph.py             capmap subgraph: bounded neighbourhood / shortest path (GraphRAG retrieval primitives)
  export/
    formats.py             GraphML / GEXF / Cypher / DOT
    viewer.py + template.html  self-contained HTML viewer (canvas + force layout, no CDN, zh/en)
  review/                  ReviewItem contract, JSONL queue, Textual TUI, emitters
  llm/client.py            unified multi-provider interface (optional LLM extraction path)
  agent/                   capmap agent: conversational operation, see the section above
    tools.py                pure-function implementations of the 14 tools + OpenAI function-calling schema wrappers
    prompt.py                system prompt (reuses the instance's CLAUDE.md)
    runtime.py                loads .env.llm, starts the openai backend
    backends/
      openai_backend.py      OpenAI-compatible endpoints (OpenAI itself, Portkey, LiteLLM, Claude, …)
  project.py               Project path resolution, shared by cli.py and agent/
  cli.py                   the capmap commands
  template/                new-instance scaffold (CLAUDE.md + 7 skills + schema + config)

instances/
  acme-corp/               the worked fictional example, see "Status" above
  <your-instance>/         generated by capmap init
    vocabulary/            instance-specific vocabulary
    scripts/bootstrap.py   deterministic cold start (the template ships a generic one; or write your own, see docs/new-instance.md)
```

`review/` (models + queue + emitters + tui) and `llm/client.py` are the genuinely domain-agnostic
infrastructure — they know nothing about any concrete node type, and any project built on this
framework can reuse them directly.

---

## Two extraction paths

**Deterministic first.** Dependency manifests, CI configuration, repo metadata and directory
structure are all machine-readable; extracting them in code is precise, free and re-runnable.
`Repo` / `Team` / `Domain` / `TechStack` / `InternalSystem` and all their edges go this way.

**The LLM only handles what code cannot see.** Architectural patterns, what a repo is *for*, what
is worth taking from it — those live in prose. `Capability` and `Pattern` belong to the LLM path
(`capmap ingest --llm`, needs a key).

In an instance that only runs the deterministic path, the `Capability` and `Pattern` node counts
will be 0. That is by design, not an omission — when the `gaps` view detects that a type has no
nodes at all, it excludes that type's vocabulary terms from the view entirely and explains why,
rather than reporting them as "nobody has these capabilities".

---

## What this graph cannot see

Every capability map has the same blind spots. They are written down here so nobody assumes they
don't exist:

- **Undocumented repos are systematically undervalued.** The tech stack can be extracted in full
  from dependency manifests; capabilities and patterns cannot. So this graph underrates teams that
  don't write READMEs.
- **Vocabulary coverage decides everything.** Anything outside the vocabulary shows up as
  "ungrounded", not as a "gap", and goes to the review queue or to
  `kg/vocabulary-suggestions.md` for a human to judge.
- **Stack overlap is a weak proxy for duplicated purpose.** Forks and copies usually dominate the
  `duplicates` view's candidates; genuine cross-team duplicate effort is the minority, and needs
  human confirmation.
- **A repo is not a capability.** Someone can be very good at something and have never committed
  code here.
- **A namespace is not necessarily a team.** If the data source cannot distinguish team from
  personal namespaces (a GitLab snapshot without `namespace.kind`, say), every per-team count is
  affected — wherever `bootstrap.py` cannot tell, it should say so rather than guess.

---

## Reference

- Architecture notes: [`docs/architecture.md`](docs/architecture.md) (Chinese)
- Creating a new instance: [`docs/new-instance.md`](docs/new-instance.md) (Chinese)
