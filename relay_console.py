#!/usr/bin/env python3
"""Small thread-safe live console for Relay scripts."""
from __future__ import annotations

import shutil
import sys
import threading
import time
from datetime import datetime
from typing import Callable, TextIO

SPINNER = "|/-\\"


class Console:
    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        interval: float = .2,
        wait_interval: float = 300,
        monotonic: Callable[[], float] = time.monotonic,
        width: Callable[[], int] | None = None,
    ) -> None:
        self.stream = stream
        self.interval = interval
        self.wait_interval = wait_interval
        self.monotonic = monotonic
        self.width = width or (lambda: shutil.get_terminal_size(fallback=(80, 24)).columns)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.text = ""
        self.last_text = ""
        self.last_wait = float("-inf")
        self.drawn = 0
        self.frame = 0

    @property
    def output(self) -> TextIO:
        return self.stream or sys.stderr

    @property
    def tty(self) -> bool:
        return bool(self.output.isatty())

    def _event(self, event: str, detail: str = "", **fields: object) -> str:
        values = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
        suffix = " ".join(part for part in (detail, values) if part)
        return f"{datetime.now():%H:%M:%S}  {event:<10} {suffix}".rstrip()

    def _clear_locked(self) -> None:
        if self.tty and self.drawn:
            self.output.write("\r" + " " * self.drawn + "\r")
            self.output.flush()
            self.drawn = 0

    def _draw_locked(self) -> None:
        if not self.text or not self.tty:
            return
        limit = max(1, self.width() - 1)
        line = f"{SPINNER[self.frame % 4]} {self.text}"[:limit]
        self.frame += 1
        padding = " " * max(0, self.drawn - len(line))
        self.output.write("\r" + line + padding)
        self.output.flush()
        self.drawn = len(line)

    def _animate(self) -> None:
        while not self.stop.wait(self.interval):
            with self.lock:
                if self.tty:
                    self._draw_locked()
                elif self.text and self.monotonic() - self.last_wait >= self.wait_interval:
                    print(self._event("WAIT", self.text), file=self.output, flush=True)
                    self.last_wait = self.monotonic()

    def update(self, text: str) -> None:
        with self.lock:
            now = self.monotonic()
            changed = text != self.last_text
            self.text = text
            if self.thread is None or not self.thread.is_alive():
                self.stop.clear()
                self.thread = threading.Thread(target=self._animate, name="relay-console", daemon=True)
                self.thread.start()
            if not self.tty:
                if changed or now - self.last_wait >= self.wait_interval:
                    print(self._event("WAIT", text), file=self.output, flush=True)
                    self.last_wait = now
                self.last_text = text
                return
            self.last_text = text
            self._draw_locked()

    def emit(self, event: str, detail: str = "", **fields: object) -> None:
        with self.lock:
            self._clear_locked()
            print(self._event(event, detail, **fields), file=self.output, flush=True)
            self._draw_locked()

    def close(self) -> None:
        with self.lock:
            self.stop.set()
            thread = self.thread
            self.thread = None
            self._clear_locked()
            self.text = ""
            self.last_text = ""
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(1, self.interval * 2))


_CONSOLE = Console()


def update(text: str) -> None:
    _CONSOLE.update(text)


def emit(event: str, detail: str = "", **fields: object) -> None:
    _CONSOLE.emit(event, detail, **fields)


def close() -> None:
    _CONSOLE.close()


def interactive() -> bool:
    return _CONSOLE.tty
