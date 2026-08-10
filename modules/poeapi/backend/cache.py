"""The response cache.

Small on purpose. Its job is not to be fast — it is to make sure a rate-limited or
failed fetch can still put *something* honest on screen, and to make it impossible
to poll ``get-characters``, which is the tightest endpoint on the account
(``10:60``, ``50:1800`` — SPEC §4.4).

Entries hold the **raw** endpoint payload rather than normalized models. Two
reasons: normalization is cheap and pure, so re-running it costs nothing; and a
change to the normalized model must not be able to make an old cache file decode
into a plausible-but-wrong item. The cached bytes always mean exactly what GGG sent.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from runtime.log import get_logger
from runtime.storage import Storage

__all__ = ["CacheEntry", "ResponseCache"]

_log = get_logger("module.poeapi.cache")

CACHE_VERSION = 1


@dataclass(frozen=True, slots=True)
class CacheEntry:
    key: str
    payload: Any
    fetched_at: float
    """Wall-clock epoch seconds, so the age survives a restart."""

    def age(self, now: float) -> float:
        return max(0.0, now - self.fetched_at)


class ResponseCache:
    """Namespaced, on-disk, one file per key."""

    def __init__(self, storage: Storage, *, clock: Callable[[], float] | None = None) -> None:
        self._storage = storage
        self._clock = clock or time.time
        self._memory: dict[str, CacheEntry] = {}

    def now(self) -> float:
        return self._clock()

    @staticmethod
    def filename(key: str) -> str:
        """Hashed, because keys contain account and tab names and storage keys are
        restricted to ``[A-Za-z0-9._-]``."""
        return hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json"

    def get(self, key: str) -> CacheEntry | None:
        cached = self._memory.get(key)
        if cached is not None:
            return cached
        try:
            data = self._storage.read_json(self.filename(key))
        except Exception as exc:  # a corrupt cache file must never be fatal
            _log.warning("discarding unreadable cache entry: %s", exc)
            return None
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return None
        fetched_at = data.get("fetched_at")
        if not isinstance(fetched_at, (int, float)):
            return None
        entry = CacheEntry(key=key, payload=data.get("payload"), fetched_at=float(fetched_at))
        self._memory[key] = entry
        return entry

    def put(self, key: str, payload: Any) -> CacheEntry:
        entry = CacheEntry(key=key, payload=payload, fetched_at=self.now())
        self._memory[key] = entry
        self._storage.write_json(
            self.filename(key),
            {"version": CACHE_VERSION, "fetched_at": entry.fetched_at, "payload": payload},
        )
        return entry

    def age(self, key: str) -> float | None:
        entry = self.get(key)
        return None if entry is None else entry.age(self.now())

    def invalidate(self, key: str) -> None:
        self._memory.pop(key, None)
        self._storage.delete(self.filename(key))

    def clear(self) -> None:
        self._memory.clear()
        self._storage.clear()
