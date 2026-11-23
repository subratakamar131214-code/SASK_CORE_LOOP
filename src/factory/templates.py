"""Static templates for zero-cost open-core products (stdlib only)."""

from __future__ import annotations

from collections.abc import Mapping

from src.core.errors import ConfigError
from src.factory.spec import ProductSpec

_LICENSE_TEXT = """\
MIT License

Copyright (c) 2026 SASK_CORE_LOOP contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _pyproject(spec: ProductSpec) -> str:
    description = _toml_escape(spec.description)
    return f"""\
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "{spec.product_name}"
version = "{spec.version}"
description = "{description}"
readme = "README.md"
license = {{ text = "{spec.license_id}" }}
requires-python = ">=3.11"
authors = [{{ name = "SASK_CORE_LOOP contributors" }}]
dependencies = []

[project.scripts]
{spec.product_name} = "{spec.package_name}.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
"""


def _package_init(spec: ProductSpec) -> str:
    return f'''\
"""{spec.product_name}: {spec.description}"""

from __future__ import annotations

__version__ = "{spec.version}"
'''

_CI_CLASSIFIER = '''\
"""Deterministic CI log classifier (open core, stdlib only)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from __PACKAGE__.models import Finding

_RULES = (
    (
        "dependency",
        re.compile(
            r"(no matching distribution|could not resolve|dependency resolution|"
            r"requirements\\.txt|npm err!|peer dep|lockfile)",
            re.IGNORECASE,
        ),
        "Pin or refresh dependency versions and lockfiles.",
    ),
    (
        "permissions",
        re.compile(
            r"(permission denied|access denied|forbidden|"
            r"resource not accessible|403)",
            re.IGNORECASE,
        ),
        "Check workflow token scopes and repository permissions.",
    ),
    (
        "oom",
        re.compile(
            r"(out of memory|oomkill|memoryerror|killed process|exit code 137)",
            re.IGNORECASE,
        ),
        "Reduce memory use or split the job into smaller steps.",
    ),
    (
        "timeout",
        re.compile(
            r"(timed out|timeout|deadline exceeded|exceeded the maximum)",
            re.IGNORECASE,
        ),
        "Cache work, raise the timeout, or split long-running steps.",
    ),
    (
        "network",
        re.compile(
            r"(connection refused|connection reset|temporary failure in name resolution|"
            r"tls handshake|socket hang up|etimedout)",
            re.IGNORECASE,
        ),
        "Retry the step and verify network access and registry status.",
    ),
    (
        "tests",
        re.compile(
            r"(assertionerror|failed tests?|test failed|pytest|jest|mocha)",
            re.IGNORECASE,
        ),
        "Open the first failing test and reproduce it locally.",
    ),
)


@dataclass(frozen=True)
class Classifier:
    max_evidence: int = 3

    def classify(self, log_text: str) -> list[Finding]:
        lines = [line.strip() for line in log_text.splitlines() if line.strip()]
        findings: list[Finding] = []
        for category, pattern, hint in _RULES:
            matches = [line[:240] for line in lines if pattern.search(line)]
            if not matches:
                continue
            confidence = min(1.0, 0.55 + 0.1 * len(matches))
            findings.append(
                Finding(
                    category=category,
                    confidence=round(confidence, 2),
                    evidence=tuple(matches[: self.max_evidence]),
                    hint=hint,
                )
            )
        if not findings:
            evidence = tuple(line[:240] for line in lines[: self.max_evidence])
            findings.append(
                Finding(
                    category="unknown",
                    confidence=0.3,
                    evidence=evidence,
                    hint="Inspect the first failing step manually; no known signature matched.",
                )
            )
        return sorted(findings, key=lambda finding: (-finding.confidence, finding.category))


def classify(log_text: str, *, max_evidence: int = 3) -> list[Finding]:
    return Classifier(max_evidence=max_evidence).classify(log_text)
'''

_CI_MODELS = '''\
"""Data models for classifier findings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Finding:
    category: str
    confidence: float
    evidence: tuple[str, ...]
    hint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "hint": self.hint,
        }
'''

_CI_CLI = '''\
"""Command line entry point for the open-core classifier."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from __PACKAGE__.classifier import classify


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify a failed CI log deterministically.")
    parser.add_argument("--log", type=Path, default=None, help="log file; stdin when omitted")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)
    if args.log is None:
        text = sys.stdin.read()
    else:
        text = args.log.read_text(encoding="utf-8", errors="replace")
    findings = classify(text)
    if args.json:
        payload = [finding.to_dict() for finding in findings]
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for finding in findings:
            print(f"[{finding.confidence:.2f}] {finding.category}: {finding.hint}")
            for line in finding.evidence:
                print(f"  - {line}")
    return 0
'''

_CI_MAIN = '''\
"""Allow python -m __PACKAGE__."""

from __future__ import annotations

from __PACKAGE__.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
'''

_OPENAPI_DIFF = '''\
"""Deterministic OpenAPI breaking-change report (open core, stdlib only)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_METHODS = {"get", "put", "post", "delete", "patch", "head", "options"}


@dataclass(frozen=True)
class BreakingChange:
    kind: str
    location: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "location": self.location, "detail": self.detail}


@dataclass(frozen=True)
class DiffReport:
    base_operations: int
    head_operations: int
    changes: tuple[BreakingChange, ...]

    @property
    def breaking(self) -> bool:
        return bool(self.changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_operations": self.base_operations,
            "head_operations": self.head_operations,
            "breaking": self.breaking,
            "changes": [change.to_dict() for change in self.changes],
            "note": "Static comparison only; review before publishing an API change.",
        }


def _operations(document: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return {}
    operations: dict[tuple[str, str], dict[str, Any]] = {}
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method.lower() in _METHODS and isinstance(operation, dict):
                operations[(str(path), method.lower())] = operation
    return operations


def _parameters(operation: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    raw = operation.get("parameters")
    if not isinstance(raw, list):
        return {}
    params: dict[tuple[str, str], dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        location = item.get("in")
        if isinstance(name, str) and isinstance(location, str):
            params[(name, location)] = item
    return params


def _schema(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    nested = value.get("schema")
    if isinstance(nested, dict):
        return nested
    content = value.get("content")
    if isinstance(content, dict):
        for media in content.values():
            if isinstance(media, dict) and isinstance(media.get("schema"), dict):
                return media["schema"]
    return value


def _required(value: Any) -> set[str]:
    schema = _schema(value)
    raw = schema.get("required")
    if not isinstance(raw, list):
        return set()
    return {item for item in raw if isinstance(item, str)}


def _properties(value: Any) -> set[str]:
    schema = _schema(value)
    raw = schema.get("properties")
    if not isinstance(raw, dict):
        return set()
    return {str(key) for key in raw}


def _parameter_type(parameter: dict[str, Any]) -> str:
    schema = parameter.get("schema")
    if isinstance(schema, dict) and isinstance(schema.get("type"), str):
        return str(schema["type"])
    return ""


def diff_documents(base: dict[str, Any], head: dict[str, Any]) -> DiffReport:
    base_ops = _operations(base)
    head_ops = _operations(head)
    changes: list[BreakingChange] = []
    for key in sorted(set(base_ops) - set(head_ops)):
        changes.append(
            BreakingChange("removed_operation", f"{key[1].upper()} {key[0]}", "Operation removed.")
        )
    for key in sorted(set(base_ops) & set(head_ops)):
        location = f"{key[1].upper()} {key[0]}"
        base_op = base_ops[key]
        head_op = head_ops[key]
        changes.extend(_compare_operation(location, base_op, head_op))
    return DiffReport(
        base_operations=len(base_ops),
        head_operations=len(head_ops),
        changes=tuple(sorted(changes, key=lambda change: (change.location, change.kind))),
    )


def _compare_operation(
    location: str, base: dict[str, Any], head: dict[str, Any]
) -> list[BreakingChange]:
    changes: list[BreakingChange] = []
    base_params = _parameters(base)
    head_params = _parameters(head)
    for key in sorted(set(base_params) - set(head_params)):
        changes.append(
            BreakingChange(
                "removed_parameter",
                f"{location} parameter {key[1]}:{key[0]}",
                "Clients that send this parameter will break.",
            )
        )
    for key in sorted(set(base_params) & set(head_params)):
        old_type = _parameter_type(base_params[key])
        new_type = _parameter_type(head_params[key])
        if old_type and new_type and old_type != new_type:
            changes.append(
                BreakingChange(
                    "parameter_type_changed",
                    f"{location} parameter {key[1]}:{key[0]}",
                    f"Type changed from {old_type} to {new_type}.",
                )
            )
    added_required = _required(head.get("requestBody")) - _required(base.get("requestBody"))
    for name in sorted(added_required):
        changes.append(
            BreakingChange(
                "required_request_field_added",
                f"{location} request body field {name}",
                "Existing clients do not send this newly required field.",
            )
        )
    base_responses = base.get("responses")
    head_responses = head.get("responses")
    if isinstance(base_responses, dict) and isinstance(head_responses, dict):
        for status in sorted(set(base_responses) & set(head_responses)):
            removed = _properties(base_responses[status]) - _properties(head_responses[status])
            for name in sorted(removed):
                changes.append(
                    BreakingChange(
                        "response_field_removed",
                        f"{location} response {status} field {name}",
                        "Consumers reading this field will receive no value.",
                    )
                )
    return changes
'''

_OPENAPI_IO = '''\
"""Document loading helpers for JSON and optional YAML inputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_document(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise SystemExit("YAML input requires PyYAML; convert the file to JSON.") from exc
        data = yaml.safe_load(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"document root must be an object: {path}")
    return data
'''

_OPENAPI_CLI = '''\
"""Command line entry point for the OpenAPI migration report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from __PACKAGE__.diff import DiffReport, diff_documents
from __PACKAGE__.io import load_document


def _markdown(report: DiffReport) -> str:
    lines = [
        "# OpenAPI migration report",
        "",
        f"- Base operations: {report.base_operations}",
        f"- Head operations: {report.head_operations}",
        f"- Breaking changes: {len(report.changes)}",
        "",
    ]
    if not report.changes:
        lines.append("No breaking changes detected by the static checks.")
        return "\\n".join(lines) + "\\n"
    for change in report.changes:
        lines.append(f"- **{change.kind}** at `{change.location}`: {change.detail}")
    return "\\n".join(lines) + "\\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare two OpenAPI documents.")
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--head", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = diff_documents(load_document(args.base), load_document(args.head))
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(_markdown(report), end="")
    return 1 if report.breaking else 0
'''

_OPENAPI_MAIN = '''\
"""Allow python -m __PACKAGE__."""

from __future__ import annotations

from __PACKAGE__.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
'''

_TEMPLATE_FILES = {
    "ci_failure_diagnoser": {
        "src/__PACKAGE__/classifier.py": _CI_CLASSIFIER,
        "src/__PACKAGE__/models.py": _CI_MODELS,
        "src/__PACKAGE__/cli.py": _CI_CLI,
        "src/__PACKAGE__/__main__.py": _CI_MAIN,
    },
    "openapi_schema_migrator": {
        "src/__PACKAGE__/diff.py": _OPENAPI_DIFF,
        "src/__PACKAGE__/io.py": _OPENAPI_IO,
        "src/__PACKAGE__/cli.py": _OPENAPI_CLI,
        "src/__PACKAGE__/__main__.py": _OPENAPI_MAIN,
    },
}


def render_template_files(spec: ProductSpec) -> dict[str, str]:
    """Render source files for a supported product type."""
    templates = _TEMPLATE_FILES.get(spec.product_type)
    if templates is None:
        supported = ", ".join(sorted(_TEMPLATE_FILES))
        raise ConfigError(
            f"unsupported product_type {spec.product_type!r}; supported: {supported}"
        )
    rendered: dict[str, str] = {
        "pyproject.toml": _pyproject(spec),
        "LICENSE": _LICENSE_TEXT,
        f"src/{spec.package_name}/__init__.py": _package_init(spec),
    }
    for path, template in templates.items():
        rendered[path.replace("__PACKAGE__", spec.package_name)] = template.replace(
            "__PACKAGE__", spec.package_name
        )
    return rendered


def base_files(spec: ProductSpec) -> Mapping[str, str]:
    return render_template_files(spec)

