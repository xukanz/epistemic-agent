# Skill: Bootstrap Instance

Turn the organisation's actual inventory into `scripts/bootstrap.py`, without
guessing anything the data doesn't say.

## When to Use

First-run protocol step 5 (`CLAUDE.md`), once the inventory source is known
and no KG exists yet. Also when a new, differently-shaped source is added to
an existing instance.

## Try the generic script first

The template ships a working `scripts/bootstrap.py` that reads a flat,
one-record-per-repo source through a `bootstrap:` block in
`config/project.yaml` — no Python required. It fits whenever the inventory is
already (or can be trivially reshaped into) a list where each entry describes
one repo: path, team, domain, a tech list, maybe internal systems.

1. Ask what format the inventory export comes in (YAML, JSON, CSV, a
   spreadsheet saved as CSV). `bootstrap.format` supports all three.
2. Map the instance's actual field names onto the generic ones in
   `bootstrap.fields` — see the commented block in `config/project.yaml` for
   the full field list and what each one does. Only list the fields that
   differ from the defaults.
3. If the export doesn't already look like a flat list (e.g. it's grouped by
   team, or repos are nested under domains), a short one-off preprocessing
   step that reshapes it into `list_key` + flat records is still "using the
   generic script" — prefer that over writing a custom bootstrap.
4. Run it:
   ```bash
   python scripts/bootstrap.py --out payload.bootstrap.json
   ```
   Read the printed report. `repos_without_domain` and
   `repos_without_team_field` are not bugs — they mean the source didn't say,
   and the generic script does not guess. If either number is unexpectedly
   high, the field mapping is probably wrong, not the data.

Stop here if this covers the source. Most flat inventories do.

## When the generic script does not fit

Some sources genuinely need code: several artefacts that must be joined by
repo path, a markdown table to parse, commit-bot filtering, or hostnames that
need resolving against the vocabulary *during* bootstrap (not just at ingest
time) — see `instances/acme-gitlab-agents/scripts/bootstrap.py` for a worked
example of all four.

Write `scripts/bootstrap.py` by hand, following the same three rules that file
follows:

1. **Extract with code wherever code can.** Dependency lists, CI config, repo
   metadata are machine-readable. No LLM call belongs in this script —
   `Capability` and `Pattern` are the only things that legitimately need one,
   and they are the ingest LLM path's job, not bootstrap's. Emitting empty or
   guessed values instead makes the map look more complete than it is.
2. **Never guess a URL or API endpoint.** Ask where the inventory export
   actually comes from. A wrong endpoint produces an empty graph silently.
3. **Say what you can't determine, instead of guessing.** If the source
   doesn't carry `namespace.kind`, don't decide group-vs-personal from repo
   count or naming heuristics — mark it `kind: unknown` and print the count.
   Every team-scoped view is corrupted by a wrong guess here, invisibly.

Reuse, don't reimplement:

- `epistemic_agent.merge.canonical` — `canonical_repo_id` / `canonical_team_id`
  / `canonical_domain_id` / `canonical_tech_id` / `canonical_system_id`,
  `team_namespace`, `split_multi_value`, `is_noise`, `normalise_tech_label`.
  These already encode the ID and normalisation conventions the rest of the
  framework depends on.
- `epistemic_agent.onto.client.resolve_backend` — if the bootstrap needs to
  tell an internal system from generic tech noise (see the vocabulary-first,
  noise-filter-second pattern in `acme-gitlab-agents/scripts/bootstrap.py`),
  consult the vocabulary instead of hard-coding a list.
- `epistemic_agent.bootstrap.generic.run_generic_bootstrap` — even a custom
  script can call this for the parts of its source that *are* flat, and only
  hand-write the join/parse logic for the parts that aren't.

Hostnames are identifiers, not labels — resolve them by exact alias lookup
only, never fuzzy matching (two hostnames sharing a domain suffix are not the
same system).

## Either way

Run the script, read the stats report out loud before moving on, then:

```bash
capmap ingest payload.bootstrap.json
```

If the vocabulary is still empty (the normal state for a brand-new instance),
expect most `TechStack`/`InternalSystem` labels to land ungrounded. That's
`skills/seed-vocabulary.md`'s job, not bootstrap's — don't try to fix it here.
