"""Durable per-job transfer checkpoints."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Iterable


class CheckpointJournal:
    """Append-only verified-file journal; a truncated final record is ignored."""

    _SCHEMA = 1
    _FLUSH_EVERY = 128

    def __init__(self, path: str | Path, signature: str, resume: bool = False):
        self.path = Path(path)
        self.signature = signature
        self.records: dict[str, dict] = {}
        self.resumed = False
        self._pending = 0
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if resume:
            self._load()
        if not self.resumed:
            self._reset()
        self._handle = self.path.open("a", encoding="utf-8", newline="\n")

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                first_line = handle.readline()
                header = json.loads(first_line)
                if (
                    header.get("type") != "meta"
                    or header.get("schema") != self._SCHEMA
                    or header.get("signature") != self.signature
                ):
                    return
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("type") == "verified" and isinstance(record.get("rel"), str):
                        self.records[record["rel"].casefold()] = record
            self.resumed = True
        except (OSError, json.JSONDecodeError, AttributeError):
            self.records.clear()

    def _reset(self) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        header = {
            "type": "meta",
            "schema": self._SCHEMA,
            "signature": self.signature,
            "created_at": time.time(),
        }
        temporary.write_text(json.dumps(header) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)

    def matching_count(self, files: Iterable) -> int:
        count = 0
        for item in files:
            record = self.records.get(str(item.rel_path).casefold())
            if (
                record is not None
                and record.get("size") == int(item.size)
                and record.get("mtime") == float(item.mtime)
            ):
                count += 1
        return count

    def _append(self, record: dict, force_flush: bool = False) -> None:
        if self._closed:
            return
        self._handle.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
        self._pending += 1
        if force_flush or self._pending >= self._FLUSH_EVERY:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._pending = 0

    def record_stage(self, stage: str) -> None:
        self._append({"type": "stage", "stage": stage, "at": time.time()}, force_flush=True)

    def record_verified(
        self,
        rel_path: str,
        size: int,
        mtime: float,
        verification_mode: str,
        verified_hash: str = "",
    ) -> None:
        record = {
            "type": "verified",
            "rel": rel_path,
            "size": int(size),
            "mtime": float(mtime),
            "verification_mode": verification_mode,
            "hash": verified_hash,
            "at": time.time(),
        }
        self.records[rel_path.casefold()] = record
        self._append(record)

    def finish(self, status: str, verified_count: int, total_count: int, error: str = "") -> None:
        self._append({
            "type": "run",
            "status": status,
            "verified": int(verified_count),
            "total": int(total_count),
            "error": error,
            "at": time.time(),
        }, force_flush=True)

    def close(self) -> None:
        if self._closed:
            return
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()
        self._closed = True


def checkpoint_signature(job) -> str:
    identity = {
        "name": job.name,
        "source": os.path.normcase(os.path.abspath(job.source_path)),
        "destination": os.path.normcase(os.path.abspath(job.destination_path)),
        "file_filter": job.file_filter,
        "older_than_days": job.older_than_days,
        "verification_mode": job.verification_mode,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()