# Skill: Seed Vocabulary

Turn a fresh instance's flood of ungrounded labels into a real
`vocabulary/*.yaml`, without transcribing them by hand one at a time.

## When to Use

Right after the first `capmap ingest` on a new instance, when
`vocabulary/` is still empty (or nearly) and `kg/vocabulary-suggestions.md`
just got long. Also periodically on an established instance, when the
suggestions file or the review queue's grounding candidates have built up
enough to be worth a batch pass — see `docs/new-instance.md` §7 on why
batching by type beats working the queue in priority order.

## Why this isn't manual transcription anymore

`capmap vocab-draft` already did the part that's pure string matching:
grouping label spellings (`Portkey`, `portkey-ai`, `Portkey AI`) into
candidate terms. What it cannot do — and what this skill is for — is decide
whether a group is a real organisation-specific term worth keeping, which
shard it belongs in, and what its canonical label should read as.

## Steps

### 1. Generate the draft

```bash
capmap vocab-draft payload.bootstrap.json      # fresh instance, first pass
capmap vocab-draft --from-suggestions          # established instance, topping up
```

This writes `kg/vocabulary-draft/*.yaml` — never `vocabulary/` directly. Read
the summary line for each node type: N raw labels → M groups. A ratio close
to 1:1 means the source had little spelling variance; a high ratio means the
fuzzy pass did real work and is worth spot-checking.

### 2. Review every group

For each entry in the draft, open with the questions the clustering algorithm
cannot answer:

- **Is the canonical `label` right?** The generator picks the longest raw
  variant, which is a shape heuristic, not a judgement — "AWS Bedrock (npm)"
  beating "Bedrock" is exactly the kind of thing to fix here.
- **Did anything get merged that shouldn't have?** Check `_raw_variants`
  against `_hostname`. The fuzzy pass already excludes anything that looks
  like a hostname, but skim for edge cases it might have missed — an internal
  codename that happens to fuzzy-match an unrelated term, for instance.
- **Is this really an `InternalSystem`, or generic tech that happens to hold
  internal data?** `docs/new-instance.md` draws this line explicitly:
  Postgres or Bedrock holding internal data is still Postgres or Bedrock, not
  an internal system. Move a mis-typed group to the other draft file if
  needed.
- **Does this deserve its own term, or is it an alias of something already in
  the framework's 12 shards?** Check `capmap ground "<label>"` before
  creating a new term — it may already resolve to something in
  `epistemic_agent/onto/shards/`, in which case the fix is adding an alias to
  *that* term in an instance shard, not minting a new one.

When unsure — especially "is this a real internal system your organisation
runs, or a public product?" — ask the person who answered the "where does the
vocabulary come from" question in the first-run conversation. Do not guess;
an invented internal-system node is worse than an ungrounded label, because it
looks authoritative.

### 3. Finalise

Move the groups you've confirmed into `vocabulary/<shard>.yaml` (create it if
it doesn't exist yet — follow `vocabulary/README.md`'s format), dropping the
`_raw_variants` and `_hostname` debug fields, which are not part of the shard
schema:

```yaml
shard: internal-system
target_type: TechStack
label: Internal systems
terms:
  - id: is:atlas
    label: Atlas
    aliases: [atlas]
```

Delete the group from the draft file once it's moved. Delete the draft file
entirely once it's empty.

### 4. Re-run and check the effect

```bash
capmap ingest payload.bootstrap.json --force
capmap health
```

Confirm the labels you just added moved out of
`kg/vocabulary-suggestions.md` and into grounded nodes. A term that doesn't
reduce the ungrounded count usually means the alias doesn't match the label's
normalised form — check `capmap ground "<the original label>"`.

## Guardrails

- Never write directly to `vocabulary/` from the draft without reading it —
  the whole point of keeping this a separate step from `capmap vocab-draft`
  is that clustering is not curation.
- Hostnames go through exact alias match only, even after this review. Do not
  add a hostname as a "close enough" alias of a similar-looking system.
- If a group's members clearly aren't the same thing, split it back into
  separate terms rather than force-fitting the generator's grouping.
