"""A local JSONL buffer: events are written here first and sent later.

Files, all in one directory, per project:

    <project>.jsonl                      events waiting to be sent
    <project>.sending-<id>.jsonl         a batch claimed by a flush in progress
    <project>.rejected.jsonl             events the dashboard refused for good

A flush claims the waiting file by renaming it (atomic), so events added meanwhile go to
a fresh file and nothing is lost or sent twice by overlap. If a flush dies halfway, the
claimed file is picked up by the next flush after a minute. Sending an event twice is
harmless because the dashboard ignores events it already has.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

STALE_SECONDS = 60.0


class Buffer:
    def __init__(self, directory: Path, project: str):
        self.dir = Path(directory)
        self.project = project
        self.path = self.dir / f"{project}.jsonl"
        self.rejected_path = self.dir / f"{project}.rejected.jsonl"
        self._lock = threading.Lock()

    # writing ---------------------------------------------------------------------
    def append(self, event: dict) -> None:
        line = json.dumps(event, separators=(",", ":"), allow_nan=False) + "\n"
        self._append_text(self.path, line)

    def append_many(self, events: list[dict], path: Path | None = None) -> None:
        if events:
            text = "".join(json.dumps(e, separators=(",", ":"), allow_nan=False) + "\n"
                           for e in events)
            self._append_text(path or self.path, text)

    def _append_text(self, path: Path, text: str) -> None:
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, text.encode())
            finally:
                os.close(fd)

    # reading and claiming --------------------------------------------------------
    def claim(self) -> tuple[list[Path], list[dict]]:
        """Take the waiting events (and any abandoned claims). Returns (files, events)."""
        files: list[Path] = []
        with self._lock:
            if self.path.exists():
                claimed = self.dir / f"{self.project}.sending-{os.getpid()}-{time.time_ns()}.jsonl"
                try:
                    self.path.rename(claimed)
                    files.append(claimed)
                except FileNotFoundError:  # another process claimed it first
                    pass
            now = time.time()
            for stale in sorted(self.dir.glob(f"{self.project}.sending-*.jsonl")):
                if stale not in files and now - stale.stat().st_mtime > STALE_SECONDS:
                    files.append(stale)
        events: list[dict] = []
        for f in files:
            events.extend(self.read(f))
        return files, events

    @staticmethod
    def read(path: Path) -> list[dict]:
        events: list[dict] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return events
        for line in lines:
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except ValueError:  # a torn or corrupt line: skip it, keep the rest
                continue
        return events

    def discard(self, files: list[Path]) -> None:
        for f in files:
            try:
                f.unlink()
            except FileNotFoundError:
                pass

    # counts ----------------------------------------------------------------------
    def pending(self) -> int:
        total = 0
        for f in [self.path, *self.dir.glob(f"{self.project}.sending-*.jsonl")]:
            total += len(self.read(f))
        return total

    def rejected(self) -> int:
        return len(self.read(self.rejected_path))
