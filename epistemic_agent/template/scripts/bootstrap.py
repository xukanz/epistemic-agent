#!/usr/bin/env python3
"""Deterministic cold start: turn the source described by `bootstrap:` in
config/project.yaml into an ingest payload.

If your inventory is already a flat list of repo records (one entry per repo,
fields like path/team/domain/tech), this file needs no edits — describe the
shape in `config/project.yaml`'s `bootstrap:` block instead. See
`vocabulary/README.md`'s sibling, `config/project.yaml`, for the field
reference, and `skills/bootstrap-instance.md` if your source needs real
parsing (several files to join, a markdown table, hostname resolution) — that
is a job for a hand-written script, not this generic one.

    python scripts/bootstrap.py --out payload.bootstrap.json
    capmap ingest payload.bootstrap.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
INSTANCE = HERE.parent
sys.path.insert(0, str(INSTANCE.parent.parent))

from epistemic_agent.bootstrap.generic import run_generic_bootstrap  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="payload.bootstrap.json")
    ap.add_argument("--report", default="")
    args = ap.parse_args()

    config = yaml.safe_load((INSTANCE / "config" / "project.yaml").read_text())
    payload, report = run_generic_bootstrap(config, INSTANCE)

    out = Path(args.out)
    if not out.is_absolute():
        out = INSTANCE / out
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nPayload written to {out}")
    print(f"  {len(payload['nodes'])} nodes, {len(payload['edges'])} edges")
    if report["repos_without_domain"]:
        print(
            f"  WARN: {report['repos_without_domain']} repos have no domain — "
            "the duplicates view filters by domain, so they will not appear there"
        )
    if report["repos_without_team_field"]:
        print(
            f"  NOTE: {report['repos_without_team_field']} repos had no `team` field "
            "mapped — team was inferred from the repo path and marked kind=unknown"
        )
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
