"""Batch prompt construction to conserve daily request quotas."""

from __future__ import annotations

from collections.abc import Sequence

from src.sensors.sanitizer import sanitize_feedback_text


def plan_batches(
    items: Sequence[str],
    *,
    instruction: str,
    max_chars: int = 6000,
    max_items: int = 20,
) -> list[str]:
    """Pack sanitized items into bounded batches, one request per returned string."""

    if max_chars < 200 or max_items < 1:
        raise ValueError("batch bounds are too small")
    batches: list[str] = []
    current: list[str] = []
    current_chars = len(instruction)

    def flush() -> None:
        nonlocal current, current_chars
        if not current:
            return
        numbered = "\n".join(f"[{index}] {item}" for index, item in enumerate(current, start=1))
        batches.append(f"{instruction}\n\n{numbered}")
        current = []
        current_chars = len(instruction)

    for raw in items:
        item = sanitize_feedback_text(raw, max_chars=min(1500, max_chars // 4))
        if not item:
            continue
        if current and (len(current) >= max_items or current_chars + len(item) > max_chars):
            flush()
        current.append(item)
        current_chars += len(item) + 6
    flush()
    if not batches:
        batches.append(instruction)
    return batches
