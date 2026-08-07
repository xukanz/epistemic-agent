"""JSONL-backed review queue.

Queue layout (inside the project directory):
    review/
        items.jsonl       — append-only, one ReviewItem per line
        decisions.jsonl   — append-only, one Decision per line
        sessions.json     — mutable, tracks last-seen position per reviewer
        attachments/      — referenced by ReviewAttachment.path
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from epistemic_agent.review.models import Decision, ReviewItem, ReviewStatus


class ReviewQueue:
    def __init__(self, review_dir: Path):
        self.dir = review_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "attachments").mkdir(exist_ok=True)
        self._items_path = self.dir / "items.jsonl"
        self._decisions_path = self.dir / "decisions.jsonl"
        self._sessions_path = self.dir / "sessions.json"

    # ------------------------------------------------------------------
    # Write

    def append(self, item: ReviewItem) -> None:
        with self._items_path.open("a") as f:
            f.write(item.model_dump_json() + "\n")

    def append_unique(self, item: ReviewItem, known: set[str] | None = None) -> bool:
        """Append unless the same question is already queued.

        Returns whether it was written. Pass `known` so that a batch of
        emissions shares one pass over the file rather than re-reading it per
        item — a full ingest emits hundreds.
        """
        keys = known if known is not None else self.pending_keys()
        key = item.dedupe_key()
        if key in keys:
            return False
        self.append(item)
        keys.add(key)
        return True

    def pending_keys(self) -> set[str]:
        """Dedupe keys of every item that has not been decided yet."""
        return {i.dedupe_key() for i in self.list_items(status=ReviewStatus.pending)}

    def record_decision(self, decision: Decision) -> None:
        with self._decisions_path.open("a") as f:
            f.write(decision.model_dump_json() + "\n")
        self._update_session(decision.reviewer, decision.review_item_id)

    # ------------------------------------------------------------------
    # Read

    def list_items(
        self,
        status: ReviewStatus | None = ReviewStatus.pending,
        source_project: str | None = None,
        source_type: str | None = None,
        tags: list[str] | None = None,
    ) -> list[ReviewItem]:
        if not self._items_path.exists():
            return []
        decided = self._decided_ids()
        items = []
        with self._items_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                item = ReviewItem.model_validate_json(line)
                if item.id in decided:
                    item.status = ReviewStatus.decided
                if status is not None and item.status != status:
                    continue
                if source_project and item.source_project != source_project:
                    continue
                if source_type and item.source_type != source_type:
                    continue
                if tags and not set(tags).intersection(item.tags):
                    continue
                items.append(item)
        items.sort(key=lambda i: i.priority.score, reverse=True)
        return items

    def list_decisions(self) -> list[Decision]:
        if not self._decisions_path.exists():
            return []
        decisions = []
        with self._decisions_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                decisions.append(Decision.model_validate_json(line))
        return decisions

    # ------------------------------------------------------------------
    # Session state

    def _sessions(self) -> dict:
        if self._sessions_path.exists():
            return json.loads(self._sessions_path.read_text())
        return {}

    def _update_session(self, reviewer: str, last_item_id: str) -> None:
        sessions = self._sessions()
        sessions[reviewer] = {
            "last_item_id": last_item_id,
            "updated_at": datetime.now().isoformat(),
        }
        self._sessions_path.write_text(json.dumps(sessions, indent=2))

    def last_seen(self, reviewer: str) -> str | None:
        return self._sessions().get(reviewer, {}).get("last_item_id")

    # ------------------------------------------------------------------
    # Helpers

    def _decided_ids(self) -> set[str]:
        return {d.review_item_id for d in self.list_decisions()}

    def pending_count(self) -> int:
        return len(self.list_items(status=ReviewStatus.pending))
