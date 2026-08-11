"""Supported API launcher with a psycopg-compatible Uvicorn loop factory."""

from __future__ import annotations

from typing import Literal, cast

import uvicorn

from app.core.config import get_settings

UVICORN_LOOP_FACTORY = "app.uvicorn_loop:selector_event_loop_factory"


def main() -> None:
    """Run the API without importing the ASGI app before loop selection."""

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        loop=cast(Literal["none", "auto", "asyncio", "uvloop"], UVICORN_LOOP_FACTORY),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
