"""Project path resolution — the one resolver every command shares.

Every command resolves its paths from `config/project.yaml`, found by walking
up from the working directory. A script that hard-codes its own default path
or reads a config key that moved will silently fall back to that default
instead of failing — the fix is one resolver every command shares, so a
missing or misconfigured key fails loudly instead of resolving to a
plausible-looking wrong answer.

Shared by `cli.py` (the human-operated CLI) and `agent/tools.py` (the
standalone agent runtime) so the two never drift on how an instance's paths
are resolved.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
import yaml
from rich.console import Console

console = Console()


class Project:
    def __init__(self, root: Path, config: dict):
        self.root = root
        self.config = config

    @property
    def name(self) -> str:
        return self.config.get("name", self.root.name)

    def _p(self, key: str, default: str) -> Path:
        rel = (self.config.get("paths") or {}).get(key, default)
        return self.root / rel

    @property
    def kg_path(self) -> Path:
        return self._p("kg", "kg/capability-map.json")

    @property
    def schema_path(self) -> Path:
        return self._p("schema", "schema/kg-schema.yaml")

    @property
    def manifest_path(self) -> Path:
        return self._p("manifest", "data/processed/manifest.json")

    @property
    def changelog_path(self) -> Path:
        return self._p("changelog", "kg/changelog.md")

    @property
    def health_path(self) -> Path:
        return self._p("health", "kg/health-manifest.json")

    @property
    def review_dir(self) -> Path:
        return self._p("review", "review")

    @property
    def suggestions_path(self) -> Path:
        return self._p("suggestions", "kg/vocabulary-suggestions.md")

    def vocabulary(self):
        from epistemic_agent.onto.client import resolve_backend

        return resolve_backend(self.config, self.root)

    def semantic_types(self) -> set[str]:
        st = (self.config.get("health") or {}).get("semantic_types")
        if st:
            return set(st)
        from epistemic_agent.health.manifest import DEFAULT_SEMANTIC_TYPES

        return set(DEFAULT_SEMANTIC_TYPES)

    def load_kg(self) -> dict:
        if not self.kg_path.exists():
            console.print(f"[red]KG not found:[/red] {self.kg_path}")
            console.print("[dim]Run the instance bootstrap or `capmap ingest` first.[/dim]")
            raise typer.Exit(1)
        return json.loads(self.kg_path.read_text())


def find_project(start: Optional[Path] = None) -> Project:
    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        cfg = candidate / "config" / "project.yaml"
        if cfg.exists():
            return Project(candidate, yaml.safe_load(cfg.read_text()) or {})
    console.print("[red]No config/project.yaml found[/red] in this directory or any parent.")
    console.print("[dim]Run `capmap init <name>` to scaffold an instance.[/dim]")
    raise typer.Exit(1)
