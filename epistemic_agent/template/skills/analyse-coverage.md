# Skill: Analyse Coverage

Run the five views and turn them into something a decision-maker can act on.

## When to Use

Once the graph is current: no unprocessed input, review queue worked, merge
applied. Running these on a half-ingested graph produces confident nonsense.

## The five views

```bash
capmap view coverage              # what this organisation actually does
capmap view coverage --by domain
capmap view duplicates            # who may be building the same thing twice
capmap view gaps                  # what nobody here does
capmap view experts -q "LangGraph"   # who has done X
capmap view risk                  # where the organisation is fragile
```

Every view takes `--json` for further processing and `--limit 0` for all rows.

## How to read each one

### coverage
Depth per area of the stack. A shard with many repos and few teams is a
*concentration*, not a strength — the same team doing it repeatedly.
Cross-check against `risk`.

### duplicates
Candidates only. Before showing any pair to a human, check three things:

1. **Are they actually in the same domain?** The view already filters on this,
   but a wrong `IN_DOMAIN` edge makes the whole row wrong.
2. **Do the descriptions agree?** Two LangGraph+FastAPI services can be a
   clinical-data assistant and a network-config assistant. Read `value` on both.
3. **Is one obviously the ancestor of the other?** Forks and copies show up
   here with Jaccard near 1.0. That is a fork, not duplicated effort.

Only survivors of those three checks are worth raising. Emit them as review
items rather than asserting them.

### gaps
Two different statuses, and conflating them is the main way this view misleads:

- `no-repo` — nothing in the graph maps to this vocabulary term. May mean the
  organisation does not do it, **or** that the sources you ingested do not
  mention it. Say which you believe and why.
- `single-repo` — exactly one repo. A real finding: no redundancy, no internal
  comparison, nobody to review the design.

A gap is only evidence of absence if the vocabulary is complete and the
ingestion is complete. Neither is ever fully true. State the caveat.

### experts
The matchmaking view — "we're starting X, who has done it". Results are sorted
by last commit, because a 2023 repo is a much weaker answer than one touched
last month. Include the `value` column in what you show; a repo name alone does
not tell the asker whether it is worth their time.

### risk
- **bus factor = 1** — one team holds a capability. Report the capability and
  the team, not a score.
- **internal lock-in** — how many repos hang off one internal system. High
  numbers are not automatically bad; a strategic platform *should* have many
  dependents. What matters is whether the concentration was chosen or drifted
  into.

## Writing it up

Structure the report around the questions, not the views:

1. **What we can do** — coverage, top areas, with counts
2. **Where we are thin** — single-repo capabilities, bus factor
3. **Where we may be duplicating** — confirmed candidates only, with the
   reasoning that survived the three checks
4. **What we do not cover** — `no-repo` terms, with the completeness caveat
5. **What this analysis cannot see** — always present; see below

## What to say about limits

Every capability map has the same blind spots. Name them rather than letting a
reader assume they are absent:

- Repos with no documentation are under-represented in capabilities and
  patterns, but fully represented in tech stack. The map therefore
  systematically understates the ability of teams that do not write READMEs.
- Vocabulary coverage bounds everything. Anything outside the vocabulary shows
  up as ungrounded, not as a gap.
- Stack overlap is a weak proxy for duplicated purpose.
- A repo is not a capability. Someone can be excellent at something and have
  committed no code about it here.

Give the numbers behind these where possible — "N of M repos had no README" is
far more useful than "coverage may be incomplete".
