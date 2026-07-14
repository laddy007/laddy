"""Daily JSONL storage with bounded retention."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class JsonlStore:
    def __init__(self, data_dir: Path, retention_days: int) -> None:
        self.data_dir = data_dir
        self.retention_days = retention_days
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._handles: dict[tuple[str, str], Any] = {}
        self._last_cleanup_day: str | None = None

    def append(
        self, kind: str, record: dict[str, Any], timestamp: float | None = None
    ) -> None:
        at = time.time() if timestamp is None else timestamp
        day = datetime.fromtimestamp(at, tz=timezone.utc).strftime("%Y-%m-%d")
        key = kind, day
        handle = self._handles.get(key)
        if handle is None:
            directory = self.data_dir / kind
            directory.mkdir(parents=True, exist_ok=True)
            handle = (directory / f"{day}.jsonl").open(
                "a", encoding="utf-8", buffering=1
            )
            self._handles[key] = handle
        handle.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
        if self._last_cleanup_day != day:
            self.cleanup(at)
            self._last_cleanup_day = day

    def cleanup(self, timestamp: float | None = None) -> None:
        at = time.time() if timestamp is None else timestamp
        cutoff = datetime.fromtimestamp(at, tz=timezone.utc).date() - timedelta(
            days=self.retention_days
        )
        for directory in self.data_dir.iterdir():
            if not directory.is_dir():
                continue
            for path in directory.glob("????-??-??.jsonl"):
                try:
                    day = datetime.strptime(path.stem, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if day < cutoff:
                    # Close and evict any cached write handle for this file
                    # before removing it: keeping a live handle to a deleted
                    # file is wrong, and on Windows unlink of an open file
                    # raises PermissionError outright.
                    handle = self._handles.pop((directory.name, path.stem), None)
                    if handle is not None:
                        handle.close()
                    path.unlink(missing_ok=True)

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()


def iter_records(data_dir: Path, kind: str, start: float, end: float):
    directory = data_dir / kind
    if not directory.is_dir():
        return
    start_day = datetime.fromtimestamp(start, tz=timezone.utc).date()
    end_day = datetime.fromtimestamp(end, tz=timezone.utc).date()
    for path in sorted(directory.glob("????-??-??.jsonl")):
        try:
            day = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < start_day or day > end_day:
            continue
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                timestamp = (
                    record.get("timestamp") if isinstance(record, dict) else None
                )
                if isinstance(timestamp, (int, float)) and start <= timestamp <= end:
                    yield record
