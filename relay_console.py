#!/usr/bin/env python3
"""Minimal shared Relay event output."""
from __future__ import annotations

import sys
from datetime import datetime


def emit(event: str, detail: str = "", **fields: object) -> None:
    values = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    suffix = " ".join(part for part in (detail, values) if part)
    print(f"{datetime.now():%H:%M:%S}  {event:<10} {suffix}".rstrip(), file=sys.stderr, flush=True)
