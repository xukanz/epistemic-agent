"""Textual terminal UI for the review workbench.

Reads pending items from the queue, presents them one at a time, and writes
decisions on keypress. Supports session resume.

Requires: pip install "epistemic-agent[tui]"
"""
from __future__ import annotations

from pathlib import Path

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.widgets import Footer, Header, Label, ListItem, ListView, Static
except ImportError as exc:
    raise ImportError(
        "Textual is required for the TUI. Install with: pip install 'epistemic-agent[tui]'"
    ) from exc

from epistemic_agent.review.models import Decision, ReviewItem, ReviewStatus
from epistemic_agent.review.queue import ReviewQueue


class ItemPane(Static):
    """Displays the current review item."""

    DEFAULT_CSS = """
    ItemPane {
        border: solid $accent;
        padding: 1 2;
        height: auto;
    }
    """

    def show(self, item: ReviewItem) -> None:
        lines = [
            f"[bold]{item.prompt.title}[/bold]",
            f"[dim]{item.source_project} / {item.source_type}[/dim]",
            "",
            f"[italic]{item.prompt.question}[/italic]",
            "",
        ]
        for ctx in item.prompt.context:
            lines.append(f"  • {ctx}")
        if item.prompt.context:
            lines.append("")
        lines.append(f"Priority: {item.priority.score:.2f} ({item.priority.strategy})")
        if item.tags:
            lines.append(f"Tags: {', '.join(item.tags)}")
        self.update("\n".join(lines))


class OptionsPane(ListView):
    """Lists the available options for the current item."""

    DEFAULT_CSS = """
    OptionsPane {
        border: solid $panel;
        height: auto;
        max-height: 12;
    }
    """

    def load(self, item: ReviewItem) -> None:
        self.clear()
        for opt in item.options:
            marker = " [suggested]" if opt.id == item.suggested_option else ""
            self.append(ListItem(Label(f"[{opt.id}] {opt.label}{marker} — {opt.description}")))


class ReviewApp(App):
    """Main review workbench application."""

    TITLE = "capmap review"
    BINDINGS = [
        Binding("s", "skip", "Skip"),
        Binding("d", "defer", "Defer"),
        Binding("q", "quit", "Quit"),
        Binding("1,2,3,4,5,6,7,8,9", "choose", "Choose option", show=False),
    ]

    def __init__(self, queue: ReviewQueue, reviewer: str = ""):
        super().__init__()
        self.queue = queue
        self.reviewer = reviewer
        self.items: list[ReviewItem] = []
        self.idx: int = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield ItemPane(id="item-pane")
        yield OptionsPane(id="options-pane")
        yield Static(id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.items = self.queue.list_items(status=ReviewStatus.pending)
        if not self.items:
            self.query_one("#status-bar", Static).update("No pending items.")
            return
        # Resume from last seen position
        last = self.queue.last_seen(self.reviewer)
        if last:
            ids = [i.id for i in self.items]
            if last in ids:
                self.idx = (ids.index(last) + 1) % len(ids)
        self._show_current()

    def _show_current(self) -> None:
        if not self.items:
            return
        item = self.items[self.idx]
        self.query_one("#item-pane", ItemPane).show(item)
        self.query_one("#options-pane", OptionsPane).load(item)
        remaining = len(self.items) - self.idx
        self.query_one("#status-bar", Static).update(
            f"Item {self.idx + 1} of {len(self.items)}  ({remaining} remaining)  "
            f"id={item.id}"
        )

    def _decide(self, choice: str) -> None:
        if not self.items:
            return
        item = self.items[self.idx]
        decision = Decision(
            review_item_id=item.id,
            decision=choice,
            reviewer=self.reviewer,
        )
        self.queue.record_decision(decision)
        self.idx += 1
        if self.idx >= len(self.items):
            self.query_one("#status-bar", Static).update("All items reviewed. Press q to quit.")
            self.query_one("#item-pane", ItemPane).update("Queue complete.")
        else:
            self._show_current()

    def action_skip(self) -> None:
        self._decide("skip")

    def action_defer(self) -> None:
        self._decide("defer")

    def action_choose(self) -> None:
        # Textual fires this for number keys; map to option index
        pass

    def on_key(self, event) -> None:
        if not self.items or self.idx >= len(self.items):
            return
        item = self.items[self.idx]
        if event.character and event.character.isdigit():
            n = int(event.character) - 1
            if 0 <= n < len(item.options):
                self._decide(item.options[n].id)


def run_tui(review_dir: Path, reviewer: str = "") -> None:
    queue = ReviewQueue(review_dir)
    app = ReviewApp(queue, reviewer=reviewer)
    app.run()
