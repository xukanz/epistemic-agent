"""Read-only view over an OpenAI-format `messages` transcript, as returned by
`run_scripted()` — one consistent way for `checks.py` to ask questions about
a conversation instead of every check re-parsing the raw list of dicts.
"""
from __future__ import annotations

import json


class Transcript:
    def __init__(self, messages: list[dict]):
        self.messages = messages

    def tool_calls(self) -> list[dict]:
        """Every tool call across the whole transcript, in order, as
        `{"name": str, "args": dict, "id": str}`."""
        out = []
        for msg in self.messages:
            if msg.get("role") != "assistant":
                continue
            for call in msg.get("tool_calls") or []:
                fn = call["function"]
                try:
                    args = json.loads(fn["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                out.append({"name": fn["name"], "args": args, "id": call["id"]})
        return out

    def tool_calls_named(self, name: str) -> list[dict]:
        return [c for c in self.tool_calls() if c["name"] == name]

    def tool_result(self, call_id: str) -> str | None:
        for msg in self.messages:
            if msg.get("role") == "tool" and msg.get("tool_call_id") == call_id:
                return msg.get("content")
        return None

    def final_texts(self) -> list[str]:
        """Every assistant message that ended a turn with no further tool
        calls — one per user turn that got a natural-language answer."""
        return [
            msg["content"]
            for msg in self.messages
            if msg.get("role") == "assistant" and not msg.get("tool_calls") and msg.get("content")
        ]

    def final_text(self) -> str:
        """The last natural-language answer in the transcript — what a check
        against "the agent's answer" almost always means."""
        texts = self.final_texts()
        return texts[-1] if texts else ""
