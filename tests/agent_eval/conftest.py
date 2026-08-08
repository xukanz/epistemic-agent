"""Shared fixtures for the LLM-in-the-loop `capmap agent` eval harness.

Two independent tiers share these:
  - the fake-LLM tier (test_fake_guardrails.py) needs a real `Project` to
    drive real tool dispatch against, but no network client;
  - the real-LLM tier (test_real_llm.py) needs the same project fixtures
    plus a real, `.env.llm`-configured OpenAI client.

No `tests/agent_eval/__init__.py` on purpose, matching the rest of `tests/`
(which has no `tests/__init__.py` either) — pytest adds this directory
straight to `sys.path`, so sibling modules (`checks`, `transcript`,
`fake_client`) import as plain top-level names.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from epistemic_agent.project import Project

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ACME_CORP = _REPO_ROOT / "instances" / "acme-corp"
_TEMPLATE = _REPO_ROOT / "epistemic_agent" / "template"


def _load_project(root: Path) -> Project:
    config = yaml.safe_load((root / "config" / "project.yaml").read_text()) or {}
    return Project(root, config)


@pytest.fixture
def acme_project(tmp_path) -> Project:
    """A private copy of the real acme-corp fixture instance.

    Scenarios that call ingest/merge/write tools must never mutate the
    committed instance under `instances/acme-corp/` — copy first, always.
    """
    dest = tmp_path / "acme-corp"
    shutil.copytree(_ACME_CORP, dest)
    return _load_project(dest)


@pytest.fixture
def fresh_project(tmp_path) -> Project:
    """A brand-new instance scaffolded from `epistemic_agent/template/`, the
    same thing `capmap init` does — no `kg/` exists yet, so every KG-touching
    tool takes the first-run-protocol path.

    Needed because acme-corp already has a KG and can't exercise that path;
    reimplemented inline rather than calling `cli.init()` directly since that
    command refuses to write into an already-existing destination, which
    `tmp_path` always is.
    """
    dest = tmp_path / "fresh-instance"
    shutil.copytree(_TEMPLATE, dest)
    for f in dest.rglob("*"):
        if f.is_file() and f.suffix in (".yaml", ".yml", ".md"):
            text = f.read_text()
            if "{{instance_name}}" in text:
                f.write_text(text.replace("{{instance_name}}", "fresh-instance"))
    return _load_project(dest)


@pytest.fixture
def big_graph_project(tmp_path) -> Project:
    """A minimal instance with a large synthetic KG — enough neighbours with
    long property text that `subgraph` must actually truncate its output.

    Same shape as `test_smoke.py`'s
    `test_subgraph_truncation_never_produces_invalid_json` regression test,
    reused here to check the *agent* narrates a truncated result rather than
    just checking the tool's JSON stays valid.
    """
    dest = tmp_path / "big-graph"
    (dest / "schema").mkdir(parents=True)
    (dest / "schema" / "kg-schema.yaml").write_text("nodes:\n  Repo: {}\nedges:\n  USES_TECH: {}\n")
    (dest / "kg").mkdir()
    big_kg = {
        "nodes": [
            {"id": "hub", "type": "Repo", "properties": {"path": "x/hub"}},
            *[
                {
                    "id": f"leaf-{i}",
                    "type": "Repo",
                    "properties": {
                        "path": f"x/leaf-{i}",
                        "description": "long free-text description " * 200,
                    },
                }
                for i in range(80)
            ],
        ],
        "edges": [
            {"id": f"e{i}", "type": "USES_TECH", "source": "hub", "target": f"leaf-{i}"}
            for i in range(80)
        ],
    }
    (dest / "kg" / "capability-map.json").write_text(json.dumps(big_kg))
    config = {
        "name": "big-graph",
        "paths": {"schema": "schema/kg-schema.yaml", "kg": "kg/capability-map.json"},
    }
    return Project(dest, config)


@pytest.fixture
def real_client():
    """A real OpenAI-compatible client built from `.env.llm`, the same
    configuration `capmap agent` itself reads.

    Skips the test (not an error) when the endpoint/key/model aren't
    configured, so `pytest -m llm_eval` degrades gracefully without
    credentials rather than failing every real-tier test.
    """
    from epistemic_agent.agent.backends.openai_backend import build_client_from_env
    from epistemic_agent.agent.runtime import _load_env_file

    _load_env_file(_REPO_ROOT)
    try:
        client, model = build_client_from_env()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    return client, model
