# Skill: Orient State

Survey the map's current state and decide what to do next.

## When to Use

First, every session. Also after any long gap.

## First-run check

If the file named by `paths.kg` in `config/project.yaml` does not exist, **stop
and follow the first-run protocol in `CLAUDE.md`.** Do not create KG files
before the schema is agreed.

## Steps

### 1. Graph shape

```bash
capmap stats
```

Note the counts by type and compare with last session. Two shapes are worth
reacting to immediately:

- **Many `Untyped` nodes** → the schema is behind the data; go to `evolve-kg`.
- **High isolated-node count** → repos got in without their edges, which
  usually means a bootstrap wrote nodes and failed before edges.

### 2. Health manifest

```bash
capmap health
```

Read every section, not just the totals:

| Signal | What it means | Where it goes |
|---|---|---|
| Ungrounded semantic nodes | vocabulary gap, or noise | `evolve-kg` / vocabulary |
| Grounding candidates | 0.50–0.70 band, needs a human | `capmap review` |
| Fuzzy duplicate pairs | same thing, two spellings | `capmap merge --dry-run` |
| Schema gap clusters | a type is missing | `evolve-kg` step 3 |
| Orphan nodes | node with no edges | usually an ingest bug |
| Stale nodes | repo with no recent human commit | a finding, not a defect |
| Bus factor = 1 | one team holds a capability | a finding |
| Duplicate-effort candidates | overlapping stacks in one domain | `capmap review` |
| Uncovered vocabulary | nothing here maps to this term | a finding, with caveats |

The bottom four are **outputs**, not problems to fix. Do not "clean them up".

### 3. Unprocessed input

```bash
capmap ingest --help    # confirm the payload path convention
find data/raw -type f | sort
```

Compare against `data/processed/manifest.json`. Files are tracked by content
hash, so a file that has not changed will not reappear.

### 4. Review queue

```bash
capmap review-status
```

### 5. Summarise and choose

Report in this shape:

```
Map: N repos, M teams, K tech, P patterns across D domains
Health: a ungrounded, b candidates, c fuzzy pairs, d schema gaps
Findings: e bus-factor-1, f duplicate candidates, g uncovered terms
Pending: h files to ingest, i review items
Next: <one action>
```

### Decision tree

- Unprocessed files → `ingest-data`
- Schema gap clusters ≥ 3 members → `evolve-kg` step 3
- Fuzzy pairs or many ungrounded → `evolve-kg` step 1, then `capmap merge --dry-run`
- Review queue non-empty → `review-status`
- Everything current → `analyse-coverage`
