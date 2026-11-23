"""Strict helpers for loading YAML mapping documents."""

from __future__ import annotations

from pathlib import Path

import yaml

from src.core.errors import ConfigError
from src.core.io import read_text


def load_yaml_mapping(path: Path) -> dict[str, object]:
    try:
        raw = yaml.safe_load(read_text(path))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    result: dict[str, object] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ConfigError(f"{path} contains a non-string key: {key!r}")
        result[key] = value
    return result


def as_mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{context} must be a mapping")
    result: dict[str, object] = {}
    for key, child in value.items():
        if not isinstance(key, str):
            raise ConfigError(f"{context} contains a non-string key: {key!r}")
        result[key] = child
    return result


def require_mapping(parent: dict[str, object], key: str, context: str) -> dict[str, object]:
    if key not in parent:
        raise ConfigError(f"{context}.{key} is required")
    return as_mapping(parent[key], f"{context}.{key}")


def optional_mapping(parent: dict[str, object], key: str, context: str) -> dict[str, object] | None:
    if key not in parent or parent[key] is None:
        return None
    return as_mapping(parent[key], f"{context}.{key}")


def require_sequence(parent: dict[str, object], key: str, context: str) -> list[object]:
    if key not in parent:
        raise ConfigError(f"{context}.{key} is required")
    value = parent[key]
    if not isinstance(value, list):
        raise ConfigError(f"{context}.{key} must be a list")
    return list(value)


def require_str(parent: dict[str, object], key: str, context: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def optional_str(parent: dict[str, object], key: str) -> str | None:
    value = parent.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string or null")
    return value.strip() or None


def require_int(parent: dict[str, object], key: str, context: str) -> int:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{context}.{key} must be an integer")
    return value


def require_float(parent: dict[str, object], key: str, context: str) -> float:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{context}.{key} must be a number")
    return float(value)


def require_bool(parent: dict[str, object], key: str, context: str) -> bool:
    value = parent.get(key)
    if not isinstance(value, bool):
        raise ConfigError(f"{context}.{key} must be a boolean")
    return value


def require_str_tuple(parent: dict[str, object], key: str, context: str) -> tuple[str, ...]:
    values = require_sequence(parent, key, context)
    result: list[str] = []
    for index, item in enumerate(values):
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{context}.{key}[{index}] must be a non-empty string")
        result.append(item.strip())
    return tuple(result)


def mapping_numbers(value: dict[str, object], context: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for key, item in value.items():
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ConfigError(f"{context}.{key} must be a number")
        result[key] = float(item)
    return result
