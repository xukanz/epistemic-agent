# acme-corp — agent instructions

This is a worked example, not a real organisation's instance. "Acme Corp" is
fictional: ~26 repos across 10 teams, invented for this repository so the
pipeline has something concrete to run against. Read `config/project.yaml`
first — it holds the purpose, every path, the vocabulary configuration, and
the merge strategy list.

Run the pipeline from this directory:

```bash
python scripts/bootstrap.py --out payload.bootstrap.json
capmap ingest payload.bootstrap.json
capmap merge
capmap health
capmap view coverage
capmap view duplicates
capmap view gaps
capmap view experts -q "langgraph"
capmap view risk
```

Regenerating the graph is safe and idempotent — delete `kg/` and
`data/processed/` and re-run the commands above to get back to a clean state.

For the first-run protocol, schema conventions, and the rules that govern any
real instance, see `../../epistemic_agent/template/CLAUDE.md` — this file only
covers what's specific to the fictional example.
