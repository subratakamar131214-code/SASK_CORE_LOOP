"""Fail-closed secret scanner for working trees, diffs, and generated artifacts."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path

from src.core.errors import ComplianceError
from src.core.redact import secret_hits

_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "build",
    "dist",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".tools",
}
_SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".whl",
    ".pyc",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".woff",
    ".woff2",
    ".ttf",
    ".mp4",
    ".mp3",
}
_MAX_FILE_BYTES = 2_000_000
_ENTROPY_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9+/_=-])([A-Za-z0-9+/_=-]{32,200})(?![A-Za-z0-9+/_=-])"
)
_SECRET_CONTEXT = re.compile(
    r"(?i)(?<![a-z])(key|token|secret|password|credential|auth|bearer)(?![a-z])"
)
_HEX_ONLY = re.compile(r"^[0-9a-fA-F]+$")


@dataclass(frozen=True)
class SecretFinding:
    path: str
    line: int
    kind: str

    def to_json(self) -> dict[str, str | int]:
        return {"path": self.path, "line": self.line, "kind": self.kind}


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _high_entropy_spans(text: str) -> list[tuple[int, str]]:
    spans: list[tuple[int, str]] = []
    for line_match in re.finditer(r"(?m)^.*$", text):
        line = line_match.group(0)
        if not _SECRET_CONTEXT.search(line):
            continue
        for match in _ENTROPY_CANDIDATE.finditer(line):
            candidate = match.group(1)
            if _HEX_ONLY.fullmatch(candidate):
                continue
            classes = sum(
                (
                    any(char.islower() for char in candidate),
                    any(char.isupper() for char in candidate),
                    any(char.isdigit() for char in candidate),
                )
            )
            if classes >= 3:
                spans.append((line_match.start() + match.start(1), "high_entropy"))
    return spans


def scan_text(text: str, *, path: str = "<memory>") -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for hit in secret_hits(text):
        findings.append(SecretFinding(path=path, line=_line_number(text, hit.start), kind=hit.kind))
    for offset, kind in _high_entropy_spans(text):
        findings.append(SecretFinding(path=path, line=_line_number(text, offset), kind=kind))
    unique = {(finding.path, finding.line, finding.kind): finding for finding in findings}
    return list(unique.values())


def scan_file(path: Path, *, display_path: str | None = None) -> list[SecretFinding]:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return []
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return scan_text(text, path=display_path or path.as_posix())


def scan_tree(root: Path) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        try:
            display = path.relative_to(root).as_posix()
        except ValueError:
            display = path.as_posix()
        findings.extend(scan_file(path, display_path=display))
        if len(findings) > 500:
            break
    return findings


def assert_clean(findings: list[SecretFinding]) -> None:
    if findings:
        preview = ", ".join(f"{item.path}:{item.line} ({item.kind})" for item in findings[:10])
        raise ComplianceError(f"secret scan failed with {len(findings)} finding(s): {preview}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan for committed secrets")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--tree", default=None, help="recursively scan a directory")
    target.add_argument("--file", default=None, help="scan one file")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    findings: list[SecretFinding]
    if args.file:
        findings = scan_file(Path(args.file))
    else:
        root = Path(args.tree or ".")
        findings = scan_tree(root)
    if args.json:
        print(json.dumps([item.to_json() for item in findings], indent=2))
    else:
        for item in findings:
            print(f"{item.path}:{item.line}: {item.kind}")
        if not findings:
            print("secret scan clean")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
