"""Decision journal (Cure 7: increase thy ability to earn).

Every rebalance is recorded with the configuration fingerprint, every gate
verdict that stopped a security, every exit decision and every order, so
each decision can be audited and learned from after the fact.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Protocol


class Journal(Protocol):
    def record(self, event: Mapping) -> None: ...


class NullJournal:
    def record(self, event: Mapping) -> None:
        pass


class MemoryJournal:
    def __init__(self) -> None:
        self.events: list[Mapping] = []

    def record(self, event: Mapping) -> None:
        self.events.append(event)


class JsonlJournal:
    """Append-only JSON Lines file: one event per line, never rewritten."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: Mapping) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")
