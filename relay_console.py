#!/usr/bin/env python3
"""Minimal shared Relay event output."""
from __future__ import annotations

import sys
import time
from datetime import datetime


def emit(event: str, detail: str = "", **fields: object) -> None:
    values = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    suffix = " ".join(part for part in (detail, values) if part)
    print(f"{datetime.now():%H:%M:%S}  {event:<10} {suffix}".rstrip(), file=sys.stderr, flush=True)


def update(text: str, *, started: float | None = None, timeout: float | None = None) -> None:
    elapsed = f" elapsed={max(0, int(time.monotonic() - started))}s/{timeout}s" if started is not None and timeout is not None else ""
    emit("WAIT", f"{text}{elapsed}")


def close() -> None:
    pass


def interactive() -> bool:
    return False
