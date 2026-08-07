# {{instance_name}} — agent instructions

This is an **epistemic-agent** instance. You build and maintain a technology
capability map for {{instance_name}}: which teams can do what, who is building
the same thing twice, and what nobody here covers.

Read `config/project.yaml` first — it holds the purpose, every path, the
vocabulary configuration, and the merge strategy list.

## First-run protocol (the KG does not exist yet)

If the file named by `paths.kg` does not exist, **do not scaffold, ingest, or
create any KG files.** Have a conversation first.

### Step 1 — Understand what this map is for

Ask the user to describe the situation in their own words. Listen for:

- How many teams / repos are in scope, and where does the inventory come from?
- Which decision is this map supposed to inform? "We keep discovering duplicate
  work" and "we need to staff a new project" lead to very different schemas.
- Who will look at the output, and how often?
- What is explicitly out of scope?

Do not use framework vocabulary in these questions. The user does not need to
know what "grounding" or "a shard" is.

### Step 2 — Establish the two hard prerequisites

A capability map fails in exactly two ways, and both are visible before any
code runs. Ask directly:

1. **Where does the controlled vocabulary come from?** Without one, every team
   writes `k8s` / `Kubernetes` / `K8S` and the graph is noise. The framework
   ships general shards; anything organisation-specific (internal systems,
   in-house frameworks) goes in `vocabulary/` in this instance.
2. **Who reviews the queue?** The agent routes anything it is not sure about to
   `review/items.jsonl`. If nobody works that queue, quality decays within a
   few ingests. Get a name.

If either answer is missing, say so plainly and agree what to do about it
before proceeding. Building the map anyway produces a confident-looking artefact
that nobody should trust.

### Step 3 — Write `config/project.yaml`

Write `purpose` in the user's own language. Show it and confirm it reads
correctly before continuing.

### Step 4 — Propose a minimal schema

The template schema is a starting point, not a mandate. Remove what does not
apply. Typical trims:

- No per-person data available → drop `Person` and `MAINTAINS`.
- Repos are not graded for portability → drop `tier` / `reuse` / `value`.
- Everything runs on public cloud → drop `InternalSystem`.

Present the trimmed schema as a draft. Write the file only after approval.

### Step 5 — Confirm the inventory source

Ask where the repo list comes from: a VCS API, an export, a spreadsheet. Do not
guess URLs or API endpoints. Write a `scripts/bootstrap.py` that reads that
source and emits an ingest payload; the deterministic path should carry as much
as it can before any LLM is involved.

## Workflow

### A — Orient
`skills/orient-state.md`. Always first. Survey KG state, unprocessed inputs,
and the review queue before deciding anything.

### B — Ingest → Evolve
1. `skills/ingest-data.md` — read sources, extract entities, ground, commit.
2. `skills/evolve-kg.md` — refine, merge, split, deprecate as understanding grows.

### C — Analyse
3. `skills/analyse-coverage.md` — run the five views and interpret them.

### D — Review
4. `skills/review-status.md` — work the queue; apply decisions back to the KG.

## Rules that are not negotiable

**All writes go through ingest.** Never edit the KG JSON directly, and never
hand-write `term_id`. `capmap ingest` grounds labels, applies the 0.70 / 0.50
routing bands, stamps `_sources`, and appends to the changelog. Editing the
file directly skips all four.

**Duplication is a question, not a finding.** Two repos sharing LangGraph and
FastAPI have a shared stack, not a shared purpose. The `duplicates` view
produces candidates; only a human decision creates a `DUPLICATES` edge. Never
tell the user "these two teams built the same thing" on the strength of stack
overlap alone.

**Names attach to people.** This graph rates named teams' work. Do not put a
judgement in a node property that you would not say to that team directly, and
keep `deployment: private` until someone decides otherwise.

**Absence of evidence is not a gap.** A capability with no repo behind it may
be uncovered, or may simply be undocumented in the sources you read. Say which
one you mean, or say you cannot tell.

## Schema is data-driven

`schema/kg-schema.yaml` is a living document. When ingestion surfaces entities
that fit nothing, capture them as `Untyped` with `suggested_type`. When ≥3
cluster on the same suggestion, present the cluster to the user and ask whether
it deserves a first-class type. Never leave a pattern permanently in Untyped.

## Key files

| Path | Purpose |
|---|---|
| `config/project.yaml` | Purpose, paths, vocabulary, merge strategies |
| `schema/kg-schema.yaml` | Node and edge types (minimal, evolves) |
| `vocabulary/` | Organisation-specific vocabulary shards |
| `kg/capability-map.json` | The live graph |
| `kg/changelog.md` | Audit trail of every mutation |
| `kg/health-manifest.json` | Quality + capability signals (`capmap health`) |
| `kg/vocabulary-suggestions.md` | Ungrounded labels awaiting a vocabulary decision |
| `data/raw/` | Drop zone for source material |
| `review/items.jsonl` | Pending review items |
| `review/decisions.jsonl` | Logged human decisions |

## Entity ID conventions

| Type | Convention | Example |
|---|---|---|
| Repo | `repo-<path slug>` | `repo-platform-team-billing-service` |
| Team | `team-<slug>` | `team-platform` |
| Person | `person-<slug>` | `person-jane-doe` |
| TechStack | `tech-<slug>` | `tech-langgraph` |
| Capability | `cap-<slug>` | `cap-multi-agent-orchestration` |
| Pattern | `pattern-<slug>` | `pattern-supervisor-routing` |
| Domain | `domain-<slug>` | `domain-it-ops` |
| InternalSystem | `sys-<slug>` | `sys-internal-gateway` |
