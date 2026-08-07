# Skill: Review Status

Work the review queue and apply the decisions back to the graph.

## When to Use

When `orient-state` reports pending items, or on a regular cadence. A queue
nobody works is worse than no queue: the agent keeps routing uncertainty into
it and the graph quietly stops being trustworthy.

## Steps

### 1. Survey

```bash
capmap review-status
```

Items are sorted by priority score. The scores mean different things per type:

| source_type | Score means | Typical action |
|---|---|---|
| `vocabulary_grounding` | how *uncertain* the match was | pick a term, or reject |
| `kg_merge` | how *similar* two nodes are | merge or keep both |
| `schema_gap` | how many nodes cluster | promote a type or not |
| `duplicate_effort` | overlap × cluster size | the judgement call |
| `multi_token` | fixed 0.6 | split or keep |
| `internal_lockin` | dependent count | accept risk or flag |

### 2. Batch by type

Do not work the queue in priority order. Work it in type order — deciding
twenty grounding candidates in a row is fast because the context is shared;
alternating between grounding and duplication decisions is slow and produces
worse decisions.

### 3. Decide

```bash
capmap review --reviewer <name>
```

Number keys pick an option, `s` skips, `d` defers, `q` quits. Progress is saved
per reviewer, so the queue can be worked in sittings.

For `duplicate_effort` items specifically, read both repos' `value` property
before deciding. The stack overlap that raised the item is not evidence about
purpose.

### 4. Apply decisions

Decisions land in `review/decisions.jsonl` — they do not change the graph by
themselves. That separation is deliberate: the queue is an append-only record
of what a human said, and applying it is a normal, logged mutation.

Translate each decision into an ingest payload:

| Decision | Payload |
|---|---|
| grounding `accept` / `pick_N` | `updates` setting `term_id` + `ontology_id` |
| grounding `reject` | `updates` clearing `_grounding_candidates` |
| merge `merge_ab` | add the pair to the instance's canonical table, re-run `capmap merge` |
| schema `add_type` | edit `schema/kg-schema.yaml`, then re-type via `updates` |
| duplicate `confirm_duplicate` | `edges` with `DUPLICATES` + `decided_by` |
| duplicate `shared_stack_only` | nothing to the graph — the decision log is the record |
| multi-token `split` | new nodes for each part, re-point edges, deprecate the `-multi` node |
| lock-in `accepted` | `updates` setting a property so it stops being re-raised |

Then:

```bash
capmap ingest decisions-payload.json
capmap health
```

### 5. Report back

Tell the user what changed and, more importantly, what the decisions revealed.
Twenty grounding rejections in one shard means the vocabulary is wrong for this
organisation, not that twenty nodes were bad. That is the finding.

## When the queue is too long

If the queue grows faster than it is worked, the thresholds are wrong for this
corpus, not the reviewer too slow. Options, in order of preference:

1. **Add vocabulary aliases.** One alias can eliminate dozens of future
   grounding items. Look at what is actually in the queue first.
2. **Raise the duplicate-effort thresholds** (`min_shared`, `min_jaccard`) if
   most decisions are `shared_stack_only` — the signal is firing too eagerly.
3. **Only then** consider narrowing what gets routed to review. Do this last:
   items stop appearing, but the uncertainty does not stop existing.
