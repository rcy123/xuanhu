"""Small bounded caches for non-authoritative, process-local graph hand-offs."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


@dataclass(frozen=True)
class _Entry(Generic[V]):
    value: V
    expires_at: float


class BoundedTTLCache(MutableMapping[K, V], Generic[K, V]):
    """A mapping-compatible LRU cache with a hard size and lifetime bound.

    Values in this cache are never authoritative.  Callers must be able to
    recover from durable claims/artifacts when an entry expires or a process
    restarts.
    """

    def __init__(self, *, max_size: int, ttl_seconds: float) -> None:
        if max_size < 1:
            raise ValueError("max_size must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._entries: OrderedDict[K, _Entry[V]] = OrderedDict()

    @property
    def max_size(self) -> int:
        return self._max_size

    def __getitem__(self, key: K) -> V:
        self._purge_expired()
        entry = self._entries[key]
        self._entries.move_to_end(key)
        return entry.value

    def __setitem__(self, key: K, value: V) -> None:
        now = time.monotonic()
        self._purge_expired(now)
        self._entries[key] = _Entry(value=value, expires_at=now + self._ttl_seconds)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_size:
            self._entries.popitem(last=False)

    def __delitem__(self, key: K) -> None:
        del self._entries[key]

    def __iter__(self) -> Iterator[K]:
        self._purge_expired()
        return iter(tuple(self._entries))

    def __len__(self) -> int:
        self._purge_expired()
        return len(self._entries)

    def _purge_expired(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= current]
        for key in expired:
            self._entries.pop(key, None)
