# Skill: Ingest Data

Read source material and grow the capability map.

## When to Use

After `orient-state` finds unprocessed input.

## Philosophy

Two extraction paths exist, and the order matters:

1. **Deterministic first.** Dependency manifests, CI configs, repo metadata and
   directory layout are machine-readable. Extracting them with code is exact,
   free, and re-runnable. Do this for everything it can cover.
2. **LLM second, for what code cannot see.** Architecture patterns, what a repo
   is *for*, what is worth reusing from it — these live in prose. Use the LLM
   only here.

Running the LLM over things a regex already knows is how a capability map gets
expensive and less accurate at the same time.

## Steps

### 1. Survey

Scan the source directory. Compare against `data/processed/manifest.json` by
content hash. Only changed or new files need work.

### 2. Triage

Classify each source before reading it closely:

- **Inventory** (repo list, API export) → many Repo/Team nodes, no prose
- **Per-repo** (README, docs) → capabilities and patterns for one repo
- **Cross-cutting** (an architecture review, a tech radar) → the collaboration
  and alternative-to edges nothing else will give you

Cross-cutting sources are the valuable ones and the easiest to skip. They are
where `COLLABORATES_WITH` and `ALTERNATIVE_TO` come from.

### 3. Extract

**Structural, deterministic:**

- `Repo` — one per repository. `path` is the natural key; never invent one.
- `Team` — from the namespace. Distinguish a real team from a personal
  namespace (`kind: group | personal`); personal namespaces inflate team counts
  and make the collaboration graph meaningless.
- `Domain` — from whatever classification the organisation already uses. Do not
  invent a taxonomy; if there isn't one, say so and leave Domain empty.
- `TechStack` — from dependency files. Split multi-value tokens
  (`anthropic/openai`), strip versions, keep the original in `raw_label`.

**Semantic, from prose:**

- `Capability` — what the repo lets someone *do*. Prefer specific over generic:
  "multi-agent fan-out with a judge stage", not "AI".
- `Pattern` — how it is built. This is the reusable part and the hardest to
  extract; it is worth the LLM call.
- `InternalSystem` — dependencies an outside team could not obtain. Record
  `replaceable` — what they would substitute — or the node is just a complaint.

### 4. Do not ground by hand

Emit raw labels. `capmap ingest` resolves them against the vocabulary and
applies the bands:

- **≥ 0.70** → `term_id`, `ontology_id`, `_grounding_score` set automatically
- **0.50–0.70** → `_grounding_candidates` set, review item emitted
- **< 0.50** → left ungrounded, label appended to `kg/vocabulary-suggestions.md`

Writing `term_id` yourself bypasses the bands and the audit trail.

### 5. Deduplicate against what exists

Check the current graph before adding. Same ID → the ingest merges properties.
Different ID, same thing → use the existing ID. Genuinely unsure → add it; that
is what `capmap merge` and the review queue are for. Do not stall on this.

### 6. Commit

```json
{
  "source_files": ["data/raw/inventory.json"],
  "nodes": [
    {"id": "repo-team-a-svc", "type": "Repo", "name": "svc",
     "path": "team-a/svc", "last_commit": "2026-07-31"},
    {"id": "tech-langgraph", "type": "TechStack", "label": "LangGraph",
     "raw_label": "langgraph"}
  ],
  "edges": [
    {"type": "USES_TECH", "source": "repo-team-a-svc", "target": "tech-langgraph"}
  ],
  "changelog_entry": "Ingested inventory: 1 repo, 1 tech"
}
```

Edge IDs are derived, so omit them.

```bash
capmap ingest payload.json
```

### 7. Check what you just did

```bash
capmap stats && capmap health
```

A jump in ungrounded nodes means the vocabulary needs terms, not that the
extraction was wrong. A jump in orphans means edges were dropped.

## Quality guidelines

- `raw_label` always preserved — normalisation is lossy and someone will ask.
- One capability per real ability, not one per marketing phrase.
- Prefer no `Domain` edge to a guessed one; a wrong domain silently corrupts
  the duplicates view, which filters by domain.
- `last_commit` should be the last **human** commit. Bot commits (dependency
  bots especially) make dead repos look alive, and staleness is a finding.
