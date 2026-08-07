# Skill: Evolve KG

Restructure and refine the map as understanding deepens. Not "add more data" —
this is where the graph stops being a pile of extractions.

## When to Use

- During ingestion, when new data contradicts or refines what is there
- Periodically, after a batch
- On request, when someone wants a restructure

## 1. Deduplication

Vocabulary-assisted first — the vocabulary already knows most equivalences:

```bash
capmap merge --dry-run
```

Read `kg/merge-report.md` before applying. Two sections matter:

**Merge map** — proposed folds. Spot-check a few. A fold of
`tech-portkey-ai` → `tech-portkey` is right; a fold of `tech-langchain-openai`
→ `tech-langchain` is a judgement call about granularity, and if the instance
cares about the distinction, the vocabulary should give it its own term.

**Invariant violations** — two nodes sharing a natural key. These are *never*
merged automatically, because it means the ingest produced them wrongly. Fix
the ingest, then re-run. Merging would hide the bug.

```bash
capmap merge
```

## 2. Granularity

The usual mistake is capabilities that are too coarse to be actionable.
"AI" is not a capability. "Multi-agent orchestration with a supervisor router"
is. Split when a node is attracting repos that do genuinely different things;
merge when two nodes always appear together and no one distinguishes them.

**Split**: create the finer nodes, re-point edges, keep the coarse node with
`SPECIALIZES` edges from fine → coarse if it still means something on its own.

**Merge**: pick the canonical ID, re-point, merge properties, log it.

## 3. Schema evolution

**The schema follows the data.** Adding a type is cheap; an unnecessary type
adds noise to every query.

**3a — capture first.** New entities that fit nothing become `Untyped` with
`suggested_type`.

**3b — ask when a cluster forms.** At ≥3 nodes sharing a `suggested_type`,
present the cluster to the user in plain language: "I keep seeing things like
X, Y, Z. They look like <SuggestedType>. Is that a real category here, or
should they fold into something existing?"

```python
from epistemic_agent.review.emitters import emit_schema_gap
from epistemic_agent.review.queue import ReviewQueue
from pathlib import Path

q = ReviewQueue(Path("review"))
q.append(emit_schema_gap(
    source_project="<instance>",
    suggested_type="Dataset",
    node_ids=["untyped-a", "untyped-b", "untyped-c"],
    node_labels=["...", "...", "..."],
))
```

**3c — after approval**, add the type to `schema/kg-schema.yaml` following the
existing property shape, then re-type the matching Untyped nodes through a
normal ingest payload.

The same protocol applies to edge types.

## 4. Vocabulary evolution

`kg/vocabulary-suggestions.md` accumulates labels nothing matched. Work it
periodically; each entry is one of three things:

- **A real term missing from the vocabulary** → add it to the right shard, with
  the aliases you saw in the wild. Re-run ingest; it will ground now.
- **An organisation-specific thing** → add it to `vocabulary/` in this
  instance, not to the framework shards.
- **Noise** — a filename, a Chinese phrase from a description, a one-off — →
  leave it. It costs nothing and the record shows the judgement was made.

Adding aliases is the highest-leverage maintenance in the whole system: one
alias line can ground dozens of nodes on the next run.

## 5. Relationship discovery

The edges nothing extracts automatically are the valuable ones:

- `COLLABORATES_WITH` — same person in two teams' repos, shared internal
  library, a cross-team repo. Always record `evidence`.
- `ALTERNATIVE_TO` — two technologies filling the same slot in different repos.
  This is what turns a stack list into a technology-choice map.
- `DUPLICATES` — **only** from a confirmed review decision. Record
  `decided_by`.

## 6. Deprecation, not deletion

Archived repo, retired system, abandoned technology: set `status: deprecated`
with `_deprecated_date` and `_deprecated_reason`. Deprecated nodes stay in the
graph and drop out of the views. Deleting loses the history of what the
organisation used to be able to do, which is exactly what someone will ask
about later.

## Holistic checklist

- [ ] Duplicates — same real thing under two IDs?
- [ ] Granularity — capabilities specific enough to act on?
- [ ] Coverage — teams in the org but not in the graph?
- [ ] Provenance — every node has `_sources`?
- [ ] Orphans — nodes with no edges?
- [ ] Personal namespaces mislabelled as teams?
- [ ] Bot commits inflating `last_commit`?
- [ ] Schema fit — an Untyped cluster forming?
