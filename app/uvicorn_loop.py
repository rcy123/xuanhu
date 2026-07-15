"""Event-loop factory for the supported Uvicorn application entry point."""

from __future__ import annotations

import asyncio


def selector_event_loop_factory() -> asyncio.AbstractEventLoop:
    """Return a psycopg-compatible loop on Windows and the default elsewhere."""

    if hasattr(asyncio, "SelectorEventLoop"):
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()  # pragma: no cover - non-CPython fallback
