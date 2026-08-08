"""Optional LLM-judge rubric grader — advisory only, never gates a
scenario's pass/fail.

Only invoked when `CAPMAP_EVAL_JUDGE=1` is set; see
`tests/agent_eval/README.md`. Scoring stays structural/tool-level by
default — prose grading is a second opinion, not a requirement.
"""
from __future__ import annotations

import os

from transcript import Transcript

JUDGE_ENV_FLAG = "CAPMAP_EVAL_JUDGE"
_VERDICTS = {"PASS", "FAIL", "UNSURE"}


def judge_enabled() -> bool:
    return os.environ.get(JUDGE_ENV_FLAG) == "1"


def grade(transcript: Transcript, rubric: str, *, client, model: str) -> tuple[str, str]:
    """Ask a model to grade the transcript's final answer against `rubric`.

    Returns `(verdict, rationale)` where verdict is one of PASS/FAIL/UNSURE.
    Never raises — a judge call failing (bad endpoint, malformed reply) is
    itself reported as an UNSURE verdict rather than failing the test run.
    """
    judge_model = os.environ.get("CAPMAP_JUDGE_MODEL", model)
    prompt = (
        "You are grading a single AI assistant's answer against a rubric. "
        "Reply with exactly one line containing only PASS, FAIL, or UNSURE, "
        "then a one-sentence rationale on the next line.\n\n"
        f"Rubric: {rubric}\n\nAssistant's final answer:\n{transcript.final_text()}"
    )
    try:
        resp = client.chat.completions.create(model=judge_model, messages=[{"role": "user", "content": prompt}])
        content = (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001 — judge failures are advisory, never fatal
        return "UNSURE", f"judge call failed: {exc}"

    lines = content.splitlines()
    verdict = lines[0].strip().upper() if lines else "UNSURE"
    if verdict not in _VERDICTS:
        verdict = "UNSURE"
    rationale = lines[1].strip() if len(lines) > 1 else content
    return verdict, rationale
