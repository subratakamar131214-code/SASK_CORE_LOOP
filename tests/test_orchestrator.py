"""End-to-end CLI tests against an isolated copy of the real configuration."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from src import orchestrator
from src.core.invariants import InvariantGuard


@pytest.fixture
def isolated_root(repo_root: Path, tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(repo_root / "config", root / "config")
    shutil.copytree(repo_root / ".github" / "workflows", root / ".github" / "workflows")
    root.mkdir(exist_ok=True)
    return root


def _run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict]:
    code = orchestrator.main(list(args))
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else {}
    return code, payload


def _write_feedback(root: Path) -> None:
    inbox = root / "inbox"
    inbox.mkdir()
    (inbox / "team.json").write_text(
        json.dumps(
            [
                {
                    "id": "gh-1",
                    "source": "github_issue",
                    "kind": "bug",
                    "title": "CI pipeline failure",
                    "body": "github actions build failure in the log",
                    "labels": ["bug"],
                    "created_at": "2026-11-20T00:00:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )


def test_cycle_runs_offline_at_zero_cost(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_feedback(isolated_root)
    code, payload = _run(
        capsys, "--root", str(isolated_root), "--dry-run", "--cycle"
    )
    assert code == 0
    assert payload["dry_run"] is True
    assert payload["online"] is False
    cycle = payload["cycle"]
    assert cycle["feedback"]["accepted"] == 1
    assert len(cycle["builds"]) == 2
    assert len(cycle["packages"]) == 2
    assert len(cycle["pages"]) == 2
    assert len(cycle["releases"]) == 2
    assert cycle["notes_providers"] == ["deterministic", "deterministic"]
    assert cycle["allocation"]["money_allocation_usd"] == 0.0
    assert cycle["allocation"]["state"] == "BOOTSTRAP"
    assert all(package["sha256"] for package in cycle["packages"])
    for artifact in ("state", "build", "dist", "swarm"):
        assert not (isolated_root / artifact).exists(), artifact


def test_status_reports_invariants(
    isolated_root: Path,
    guard: InvariantGuard,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, payload = _run(capsys, "--root", str(isolated_root), "--dry-run", "--status")
    assert code == 0
    status = payload["status"]
    assert status["invariants_digest"] == guard.digest
    assert status["billing"]["hard_cap_usd"] == 0.0
    assert status["billing"]["paid_resources_enabled"] is False
    assert status["ledger"]["chain_valid"] is True
    assert status["ledger"]["cost_usd"] == 0.0
    assert not (isolated_root / "state").exists()


def test_default_invocation_reports_status(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run(capsys, "--root", str(isolated_root), "--dry-run")
    assert code == 0
    assert "status" in payload
    assert "cycle" not in payload


def test_evolve_proposes_but_never_applies(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run(capsys, "--root", str(isolated_root), "--dry-run", "--evolve")
    assert code == 0
    evolve = payload["evolve"]
    assert evolve["verified"] is True
    assert evolve["proposals"]
    assert len(evolve["proposals"]) <= 2
    for proposal in evolve["proposals"]:
        assert proposal["auto_apply"] is False
        assert proposal["status"] == "proposed"
    assert len(evolve["gates"]) == len(evolve["proposals"])
    assert all(gate["passed"] for gate in evolve["gates"])


def test_replicate_skips_when_swarm_disabled(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run(
        capsys, "--root", str(isolated_root), "--dry-run", "--replicate"
    )
    assert code == 0
    assert payload["replicate"]["status"] == "skipped"


def test_heartbeat_cli_records_alerts(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run(
        capsys, "--root", str(isolated_root), "--dry-run", "--heartbeat"
    )
    assert code == 0
    heartbeat = payload["heartbeat"]
    assert heartbeat["status"] == "stale"
    assert heartbeat["alerts_recorded"] == 2
    assert heartbeat["chain_valid"] is True
    assert heartbeat["rollup"]["active_count"] == 0


def test_offline_flag_beats_online(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run(
        capsys,
        "--root",
        str(isolated_root),
        "--dry-run",
        "--online",
        "--offline",
        "--status",
    )
    assert code == 0
    assert payload["online"] is False


def test_missing_config_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = orchestrator.main(["--root", str(tmp_path), "--dry-run", "--status"])
    captured = capsys.readouterr()
    assert code == 2
    error = json.loads(captured.err)
    assert error["error"] in {"ConfigError", "StateError"}
