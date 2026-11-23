"""Tests for heartbeat supervision, release manifests, and the GitHub wrapper."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from src.core.config import SystemConfig
from src.core.errors import ComplianceError, StateError
from src.core.invariants import InvariantGuard
from src.core.io import sha256_file, sha256_hex
from src.core.paths import RepoPaths
from src.distribution.landing import LandingPage
from src.distribution.packager import PackageArtifact
from src.factory.builder import BuildResult
from src.ledger.ledger import Ledger
from src.ops.gh import GitHubClient, RepoRef
from src.ops.heartbeat import HeartbeatMonitor
from src.ops.release import ReleaseManager

from helpers import FrozenClock

FIXED = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def _ledger(paths: RepoPaths, guard: InvariantGuard) -> Ledger:
    ledger = Ledger(paths.ledger_file, guard)
    ledger.ensure_genesis(guard.digest)
    return ledger


def _monitor(
    paths: RepoPaths,
    config: SystemConfig,
    ledger: Ledger,
) -> HeartbeatMonitor:
    return HeartbeatMonitor(
        paths, config.heartbeat, config.swarm, ledger, dry_run=False
    )


def test_heartbeat_reports_missing_workflows(
    runtime_paths: RepoPaths, config: SystemConfig, guard: InvariantGuard
) -> None:
    monitor = _monitor(runtime_paths, config, _ledger(runtime_paths, guard))
    check = monitor.inspect()
    assert check.healthy is False
    assert any("revenue-cycle.yml" in item for item in check.stale)
    assert any("heartbeat-immortality.yml" in item for item in check.stale)
    assert any("no cycle heartbeat" in item for item in check.stale)


def test_heartbeat_caps_daily_alerts(
    runtime_paths: RepoPaths, config: SystemConfig, guard: InvariantGuard
) -> None:
    monitor = _monitor(runtime_paths, config, _ledger(runtime_paths, guard))
    report = monitor.run()
    assert report.check.healthy is False
    assert report.alerts_recorded == config.heartbeat.max_alerts_per_day
    assert report.alerts_suppressed > 0
    assert (runtime_paths.state_dir / "heartbeat.json").is_file()

    second = monitor.run()
    assert second.alerts_recorded == 0
    assert second.alerts_suppressed > 0


def test_heartbeat_records_and_validates_workflow_names(
    runtime_paths: RepoPaths, config: SystemConfig, guard: InvariantGuard
) -> None:
    monitor = _monitor(runtime_paths, config, _ledger(runtime_paths, guard))
    monitor.record_workflow_run("revenue-cycle.yml")
    workflows = runtime_paths.state_dir / "workflows.json"
    assert "revenue-cycle.yml" in workflows.read_text(encoding="utf-8")
    with pytest.raises(StateError, match="unsafe workflow"):
        monitor.record_workflow_run("../../evil.sh")


def test_heartbeat_detects_invalid_chain(
    runtime_paths: RepoPaths, config: SystemConfig, guard: InvariantGuard
) -> None:
    ledger = _ledger(runtime_paths, guard)
    monitor = _monitor(runtime_paths, config, ledger)
    text = runtime_paths.ledger_file.read_text(encoding="utf-8")
    runtime_paths.ledger_file.write_text(
        text.replace('"SASK_CORE_LOOP"', '"TAMPERED"'), encoding="utf-8"
    )
    check = monitor.inspect()
    assert check.chain_valid is False
    assert "ledger hash chain is invalid" in check.stale


def test_heartbeat_healthy_after_fresh_signals(
    runtime_paths: RepoPaths, config: SystemConfig, guard: InvariantGuard
) -> None:
    ledger = _ledger(runtime_paths, guard)
    monitor = _monitor(runtime_paths, config, ledger)
    for workflow in config.heartbeat.required_workflows:
        monitor.record_workflow_run(workflow.file)
    ledger.record_cycle({"verified": True})
    ledger.record_heartbeat({"status": "ok"})
    ledger.append("upgrade_verified", {"proposal_id": "prop-0123456789ab"})
    check = monitor.inspect()
    assert check.healthy is True, check.stale


def _build(paths: RepoPaths) -> BuildResult:
    root = paths.build_dir / "ci-failure-diagnoser"
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    return BuildResult(
        niche_id="ci-failure-diagnoser",
        product_name="sask-ci-diagnoser",
        package_name="sask_ci_diagnoser",
        product_type="ci_failure_diagnoser",
        version="0.1.0",
        root=root,
        root_display=paths.display(root),
        files=("README.md",),
        source_files=(),
        test_files=(),
        doc_files=("README.md",),
        features=("Offline classification.",),
        digest=sha256_hex("build"),
        feedback_count=1,
    )


def _package(paths: RepoPaths, build: BuildResult) -> PackageArtifact:
    archive = paths.dist_dir / build.niche_id / "sask-ci-diagnoser-0.1.0.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"zip-bytes")
    return PackageArtifact(
        niche_id=build.niche_id,
        product_name=build.product_name,
        version=build.version,
        archive=archive,
        archive_display=paths.display(archive),
        sha256=sha256_file(archive),
        size_bytes=archive.stat().st_size,
        file_count=1,
        license_id="MIT",
        created_at="2026-11-20T12:00:00Z",
    )


def _landing(paths: RepoPaths, build: BuildResult) -> LandingPage:
    page = paths.dist_dir / "site" / build.niche_id / "index.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("<html></html>", encoding="utf-8")
    return LandingPage(
        niche_id=build.niche_id,
        path=page,
        page_display=paths.display(page),
        sha256=sha256_hex("<html></html>"),
        size_bytes=len("<html></html>"),
        channel_count=0,
        created_at="2026-11-20T12:00:00Z",
    )


def test_release_prepare_is_idempotent_and_sanitized(
    runtime_paths: RepoPaths, guard: InvariantGuard
) -> None:
    ledger = _ledger(runtime_paths, guard)
    manager = ReleaseManager(runtime_paths, ledger, clock=FrozenClock())
    build = _build(runtime_paths)
    package = _package(runtime_paths, build)
    page = _landing(runtime_paths, build)
    manifest = manager.prepare(
        build, package, page, notes="Contact user@example.com for details."
    )
    assert manifest.release_id.startswith("rel-")
    assert manifest.status == "prepared"
    assert manifest.tag == "v0.1.0-ci-failure-diagnoser"
    assert "[REDACTED:EMAIL]" in manifest.notes
    assert "user@example.com" not in manifest.notes
    assert (runtime_paths.releases_dir / f"{manifest.release_id}.json").is_file()

    again = manager.prepare(build, package, page, notes="different")
    assert again.release_id == manifest.release_id
    releases = [entry for entry in ledger.entries() if entry.get("kind") == "release"]
    assert len(releases) == 1


def test_release_publish_with_dry_run_github(
    runtime_paths: RepoPaths, guard: InvariantGuard
) -> None:
    ledger = _ledger(runtime_paths, guard)
    manager = ReleaseManager(runtime_paths, ledger, clock=FrozenClock())
    build = _build(runtime_paths)
    package = _package(runtime_paths, build)
    manifest = manager.prepare(build, package, _landing(runtime_paths, build))
    published = manager.publish(
        manifest,
        GitHubClient(dry_run=True),
        RepoRef(owner="example", name="SASK_CORE_LOOP"),
    )
    assert published.status == "published"
    assert published.published_url == (
        "https://github.com/example/SASK_CORE_LOOP/releases/tag/"
        "v0.1.0-ci-failure-diagnoser"
    )
    stored = (runtime_paths.releases_dir / f"{manifest.release_id}.json").read_text(
        encoding="utf-8"
    )
    assert '"status": "published"' in stored
    kinds = [entry["payload"]["status"] for entry in ledger.entries() if entry["kind"] == "release"]
    assert kinds == ["prepared", "published"]


def test_release_publish_rejects_missing_or_external_archive(
    runtime_paths: RepoPaths, guard: InvariantGuard
) -> None:
    manager = ReleaseManager(runtime_paths, _ledger(runtime_paths, guard))
    build = _build(runtime_paths)
    package = _package(runtime_paths, build)
    manifest = manager.prepare(build, package, _landing(runtime_paths, build))
    with pytest.raises(StateError, match="outside the distribution"):
        manager.publish(
            replace(manifest, archive="README.md"),
            GitHubClient(dry_run=True),
            RepoRef(owner="example", name="SASK_CORE_LOOP"),
        )
    with pytest.raises(StateError, match="manifest is missing"):
        manager.publish(
            replace(manifest, release_id="rel-000000000000"),
            GitHubClient(dry_run=True),
            RepoRef(owner="example", name="SASK_CORE_LOOP"),
        )


def test_github_client_dry_run_and_token_checks() -> None:
    client = GitHubClient(dry_run=True)
    repo = client.create_public_repo(
        "sask-child-demo", description="demo", owner="example"
    )
    assert repo.url == "https://github.com/example/sask-child-demo"
    assert client.create_release(
        repo, tag="v0.1.0", title="t", notes="n"
    ) == "https://github.com/example/sask-child-demo/releases/tag/v0.1.0"
    with pytest.raises(StateError, match="invalid repository name"):
        client.create_public_repo("Bad Name", description="demo")
    missing = Path("definitely-missing.zip")
    with pytest.raises(StateError, match="release asset is missing"):
        client.create_release(repo, tag="v0.1.0", title="t", notes="n", assets=[missing])

    unauthenticated = GitHubClient(dry_run=False, environ={})
    with pytest.raises(ComplianceError, match="FINE_GRAINED_GH_TOKEN"):
        unauthenticated.create_public_repo("demo", description="demo")
