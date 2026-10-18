"""Small JSON state file so restarts don't re-alert or reset API budgets."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {"aircraft": {}, "usage": {}}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text())
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (OSError, ValueError) as e:
            # A corrupt state file shouldn't stop the watcher; start fresh.
            log.warning("could not read state file %s (%s); starting fresh", self.path, e)

    @property
    def aircraft(self) -> dict[str, dict[str, Any]]:
        return self.data.setdefault("aircraft", {})

    @property
    def usage(self) -> dict[str, dict[str, Any]]:
        return self.data.setdefault("usage", {})

    def prune(self, now: float, max_age_s: float = 24 * 3600) -> None:
        stale = [h for h, s in self.aircraft.items() if now - (s.get("last_seen") or 0) > max_age_s]
        for h in stale:
            del self.aircraft[h]

    def save(self) -> None:
        """Atomic write: never leaves a half-written file behind."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
