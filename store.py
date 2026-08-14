"""Persistent state: which jobs we've already posted, and who's subscribed.

Everything lives in a single JSON file so it can be committed back to the
GitHub repo between Actions runs (Actions runners are ephemeral — without
this the bot would re-post the same jobs forever).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Forget job ids older than this so state.json doesn't grow without bound.
TTL_SECONDS = 14 * 24 * 3600


class Store:
    def __init__(self, path: str | os.PathLike = "state.json"):
        self.path = Path(path)
        self.data = {"seen": {}, "subscribers": {}, "update_offset": 0}
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text()))
            except (json.JSONDecodeError, OSError):
                pass  # corrupt state -> start clean rather than crash

    # ---------------- seen jobs ----------------

    def is_new(self, job_id: str) -> bool:
        return job_id not in self.data["seen"]

    def mark_seen(self, job_id: str) -> None:
        self.data["seen"][job_id] = int(time.time())

    def prune(self) -> None:
        cutoff = int(time.time()) - TTL_SECONDS
        self.data["seen"] = {
            k: v for k, v in self.data["seen"].items() if v > cutoff
        }

    # ---------------- subscribers ----------------

    def subscribers(self) -> dict:
        return self.data["subscribers"]

    # An empty list always means "no restriction", so new subscribers
    # receive everything until they narrow it down themselves.
    DEFAULTS = {"keywords": [], "locations": [], "tracks": [], "levels": []}

    def subscribe(self, chat_id) -> bool:
        """Returns True if this is a brand-new subscriber."""
        key = str(chat_id)
        if key in self.data["subscribers"]:
            # Backfill fields added after this person subscribed, so an
            # older subscriber does not crash on a missing key.
            for k, v in self.DEFAULTS.items():
                self.data["subscribers"][key].setdefault(k, list(v))
            return False
        self.data["subscribers"][key] = {k: list(v) for k, v in self.DEFAULTS.items()}
        return True

    def unsubscribe(self, chat_id) -> None:
        self.data["subscribers"].pop(str(chat_id), None)

    def set_filter(self, chat_id, field: str, values: list[str]) -> None:
        self.subscribe(chat_id)          # ensures the record exists + is complete
        self.data["subscribers"][str(chat_id)][field] = values

    # ---------------- telegram update cursor ----------------

    @property
    def offset(self) -> int:
        return self.data.get("update_offset", 0)

    @offset.setter
    def offset(self, value: int) -> None:
        self.data["update_offset"] = value

    # ---------------- persistence ----------------

    def save(self) -> None:
        self.prune()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)          # atomic: never leave a half-written file
