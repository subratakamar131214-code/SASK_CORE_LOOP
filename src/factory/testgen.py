"""Generate pytest suites for open-core product scaffolds."""

from __future__ import annotations

from src.core.errors import ConfigError
from src.factory.spec import ProductSpec

_CI_TESTS = '''\
"""Behavioral tests for the generated CI classifier."""

from __future__ import annotations

import json

from __PACKAGE__.classifier import classify
from __PACKAGE__.cli import main


def test_dependency_failure_is_ranked_first() -> None:
    log = "npm ERR! code ERESOLVE\\nNo matching distribution found for demo"
    findings = classify(log)
    assert findings[0].category == "dependency"
    assert findings[0].confidence >= 0.55


def test_oom_is_detected() -> None:
    findings = classify("fatal: out of memory\\nKilled process 42")
    assert findings[0].category == "oom"


def test_unknown_log_is_low_confidence() -> None:
    findings = classify("all systems nominal")
    assert findings[0].category == "unknown"
    assert findings[0].confidence < 0.5


def test_cli_emits_json(tmp_path, capsys) -> None:
    log_file = tmp_path / "ci.log"
    log_file.write_text("timed out after 30 minutes\\n", encoding="utf-8")
    assert main(["--log", str(log_file), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["category"] == "timeout"
'''

_OPENAPI_TESTS = '''\
"""Behavioral tests for the generated OpenAPI migration report."""

from __future__ import annotations

import json

from __PACKAGE__.cli import main
from __PACKAGE__.diff import diff_documents

BASE = {
    "openapi": "3.0.0",
    "paths": {
        "/pets": {
            "get": {
                "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"id": {}, "name": {}},
                                }
                            }
                        },
                    }
                },
            },
            "post": {"responses": {"201": {"description": "created"}}},
        }
    },
}


def test_removed_operation_is_breaking() -> None:
    head = json.loads(json.dumps(BASE))
    del head["paths"]["/pets"]["post"]
    report = diff_documents(BASE, head)
    assert report.breaking
    assert report.changes[0].kind == "removed_operation"


def test_removed_response_field_is_breaking() -> None:
    head = json.loads(json.dumps(BASE))
    schema = head["paths"]["/pets"]["get"]["responses"]["200"]["content"]["application/json"]
    schema["schema"]["properties"] = {"id": {}}
    report = diff_documents(BASE, head)
    assert any(change.kind == "response_field_removed" for change in report.changes)


def test_parameter_type_change_is_breaking() -> None:
    head = json.loads(json.dumps(BASE))
    head["paths"]["/pets"]["get"]["parameters"][0]["schema"]["type"] = "string"
    report = diff_documents(BASE, head)
    assert any(change.kind == "parameter_type_changed" for change in report.changes)


def test_cli_returns_one_on_breaking_change(tmp_path, capsys) -> None:
    base_file = tmp_path / "base.json"
    head_file = tmp_path / "head.json"
    base_file.write_text(json.dumps(BASE), encoding="utf-8")
    empty = {"openapi": "3.0.0", "paths": {}}
    head_file.write_text(json.dumps(empty), encoding="utf-8")
    assert main(["--base", str(base_file), "--head", str(head_file)]) == 1
    assert "Breaking changes" in capsys.readouterr().out
'''

_TESTS = {
    "ci_failure_diagnoser": _CI_TESTS,
    "openapi_schema_migrator": _OPENAPI_TESTS,
}


def generate_tests(spec: ProductSpec) -> dict[str, str]:
    template = _TESTS.get(spec.product_type)
    if template is None:
        raise ConfigError(f"no test template for product_type {spec.product_type!r}")
    return {
        f"tests/test_{spec.product_type}.py": template.replace("__PACKAGE__", spec.package_name)
    }
