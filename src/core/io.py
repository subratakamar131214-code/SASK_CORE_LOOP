"""Atomic filesystem helpers, canonical JSON, hashing, and cross-process locks."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast

from src.core.errors import StateError
from src.core.types import JsonValue


def canonical_json(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def pretty_json(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def sha256_hex(data: str | bytes) -> str:
    payload = data.encode("utf-8") if isinstance(data, str) else data
    return sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateError(f"cannot read {path}: {exc}") from exc


def write_text_atomic(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, path)
    except OSError as exc:
        raise StateError(f"cannot write {path}: {exc}") from exc


def read_json(path: Path, default: JsonValue = None) -> JsonValue:
    if not path.exists():
        return default
    try:
        return cast(JsonValue, json.loads(read_text(path)))
    except json.JSONDecodeError as exc:
        raise StateError(f"invalid JSON in {path}: {exc}") from exc


def write_json_atomic(path: Path, value: JsonValue) -> None:
    write_text_atomic(path, pretty_json(value))


def read_jsonl(path: Path) -> list[JsonValue]:
    if not path.exists():
        return []
    entries: list[JsonValue] = []
    for line_number, raw in enumerate(read_text(path).splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            entries.append(cast(JsonValue, json.loads(line)))
        except json.JSONDecodeError as exc:
            raise StateError(f"invalid JSONL in {path} line {line_number}: {exc}") from exc
    return entries


def append_jsonl(path: Path, value: JsonValue) -> None:
    ensure_dir(path.parent)
    try:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise StateError(f"cannot append to {path}: {exc}") from exc


@contextmanager
def file_lock(
    path: Path,
    *,
    timeout_seconds: float = 30.0,
    stale_seconds: float = 180.0,
) -> Iterator[None]:
    """Small cross-platform exclusive lock based on atomic file creation."""

    ensure_dir(path.parent)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > stale_seconds:
                with suppress(FileNotFoundError):
                    path.unlink()
                continue
            if time.monotonic() >= deadline:
                raise StateError(f"timed out waiting for lock {path}") from None
            time.sleep(0.05)
        else:
            try:
                os.write(descriptor, str(os.getpid()).encode("ascii"))
            finally:
                os.close(descriptor)
            break
    try:
        yield
    finally:
        with suppress(FileNotFoundError):
            path.unlink()
