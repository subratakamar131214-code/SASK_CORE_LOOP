"""Tests for deterministic product builds, packaging, landing pages, and funnel events."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from src.core.config import SystemConfig
from src.core.errors import ComplianceError, ConfigError, StateError
from src.core.paths import RepoPaths
from src.distribution.funnel import STAGES, FunnelTelemetry
from src.distribution.landing import LandingPageBuilder
from src.distribution.packager import PackageBuilder
from src.factory.builder import BuildResult, ProductBuilder
from src.factory.spec import ProductSpec, package_name_for

from helpers import FrozenClock, fake_groq_key, make_feedback_item


@pytest.fixture
def builder(runtime_paths: RepoPaths) -> ProductBuilder:
    return ProductBuilder(runtime_paths, version="0.1.0", clock=FrozenClock())


def test_build_creates_expected_files(
    config: SystemConfig, builder: ProductBuilder
) -> None:
    niche = config.niche_by_id("ci-failure-diagnoser")
    build = builder.build(niche, [make_feedback_item()])
    assert build.niche_id == niche.id
    assert build.product_name == "sask-ci-diagnoser"
    assert build.package_name == "sask_ci_diagnoser"
    assert build.version == "0.1.0"
    for relative in (
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "USAGE.md",
        "PRO_FEATURES.md",
        "SECURITY.md",
        f"src/{build.package_name}/__init__.py",
        f"src/{build.package_name}/classifier.py",
        "tests/test_ci_failure_diagnoser.py",
        "sask-build.json",
    ):
        assert (build.root / relative).is_file(), relative
    assert build.feedback_count == 1
    assert (build.root / "README.md").read_text(encoding="utf-8").count("Flaky pipeline") == 1


def test_all_generated_python_compiles(
    config: SystemConfig, builder: ProductBuilder
) -> None:
    niche = config.niche_by_id("openapi-schema-migrator")
    build = builder.build(niche)
    for path in build.root.rglob("*.py"):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_generated_tests_pass(
    config: SystemConfig, builder: ProductBuilder
) -> None:
    build = builder.build(config.niche_by_id("ci-failure-diagnoser"))
    env = {**os.environ, "PYTHONPATH": str(build.root / "src")}
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=build.root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_digest_is_stable_across_rebuilds(
    config: SystemConfig, runtime_paths: RepoPaths
) -> None:
    niche = config.niche_by_id("ci-failure-diagnoser")
    feedback = [make_feedback_item()]
    first = ProductBuilder(runtime_paths, clock=FrozenClock()).build(niche, feedback)
    second = ProductBuilder(runtime_paths, clock=FrozenClock()).build(niche, feedback)
    assert first.digest == second.digest
    assert first.files == second.files


def test_package_is_deterministic_and_relative(
    config: SystemConfig, runtime_paths: RepoPaths
) -> None:
    build = ProductBuilder(runtime_paths, clock=FrozenClock()).build(
        config.niche_by_id("ci-failure-diagnoser")
    )
    packager = PackageBuilder(runtime_paths, config.compliance, clock=FrozenClock())
    first = packager.package(build)
    second = packager.package(build)
    assert first.sha256 == second.sha256
    assert first.archive.is_file()
    assert runtime_paths.dist_dir.resolve() in first.archive.resolve().parents
    with zipfile.ZipFile(first.archive) as bundle:
        names = bundle.namelist()
        assert all(name.startswith(f"{build.product_name}-{build.version}/") for name in names)
        for info in bundle.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
    sums = (first.archive.parent / "SHA256SUMS").read_text(encoding="utf-8")
    assert first.sha256 in sums


def test_package_rejects_disallowed_license(
    config: SystemConfig, runtime_paths: RepoPaths
) -> None:
    build = ProductBuilder(runtime_paths, clock=FrozenClock()).build(
        config.niche_by_id("ci-failure-diagnoser")
    )
    packager = PackageBuilder(runtime_paths, config.compliance)
    with pytest.raises(ComplianceError):
        packager.package(build, license_id="GPL-3.0")


def test_package_rejects_secret_in_build_tree(
    config: SystemConfig, runtime_paths: RepoPaths
) -> None:
    build = ProductBuilder(runtime_paths, clock=FrozenClock()).build(
        config.niche_by_id("ci-failure-diagnoser")
    )
    (build.root / "notes.txt").write_text(f"token={fake_groq_key()}\n", encoding="utf-8")
    packager = PackageBuilder(runtime_paths, config.compliance)
    with pytest.raises(ComplianceError):
        packager.package(build)


def test_landing_page_is_static_csp_locked(
    config: SystemConfig, runtime_paths: RepoPaths
) -> None:
    build = ProductBuilder(runtime_paths, clock=FrozenClock()).build(
        config.niche_by_id("ci-failure-diagnoser")
    )
    page = LandingPageBuilder(runtime_paths, clock=FrozenClock()).render(
        build,
        config.niche_by_id("ci-failure-diagnoser"),
        config.monetization.channels,
        archive_name="sask-ci-diagnoser-0.1.0.zip",
    )
    text = page.path.read_text(encoding="utf-8")
    assert "default-src 'none'" in text
    assert "<script" not in text.lower()
    assert "google-analytics" not in text.lower()
    assert "planned (human setup required)" in text
    assert page.channel_count == len(config.monetization.channels)
    assert page.sha256 == LandingPageBuilder(runtime_paths, clock=FrozenClock()).render(
        build,
        config.niche_by_id("ci-failure-diagnoser"),
        config.monetization.channels,
        archive_name="sask-ci-diagnoser-0.1.0.zip",
    ).sha256


def test_landing_page_without_channels_uses_feedback_hint(
    config: SystemConfig, runtime_paths: RepoPaths
) -> None:
    build = ProductBuilder(runtime_paths, clock=FrozenClock()).build(
        config.niche_by_id("openapi-schema-migrator")
    )
    page = LandingPageBuilder(runtime_paths).render(
        build, config.niche_by_id("openapi-schema-migrator"), ()
    )
    text = page.path.read_text(encoding="utf-8")
    assert "Star the repository" in text
    assert page.channel_count == 0


def test_unsupported_product_type_is_rejected() -> None:
    spec = ProductSpec.create(
        niche_id="x",
        product_name="demo",
        product_type="unknown_type",
        description="d",
        version="0.1.0",
        features=("f",),
        pro_features=(),
    )
    from src.factory.templates import render_template_files

    with pytest.raises(ConfigError):
        render_template_files(spec)


def test_package_name_slug_rules() -> None:
    assert package_name_for("sask-ci-diagnoser") == "sask_ci_diagnoser"
    assert package_name_for("9lives") == "product_9lives"
    assert package_name_for("!!!") == "product"


def test_funnel_records_and_summarizes(tmp_path: Path) -> None:
    telemetry = FunnelTelemetry(tmp_path, clock=FrozenClock())
    telemetry.record("ci-failure-diagnoser", "impression", source="site", count=10)
    telemetry.record("ci-failure-diagnoser", "artifact_download", source="site", count=5)
    telemetry.record("ci-failure-diagnoser", "feedback_signal", source="inbox", count=2)
    telemetry.record("ci-failure-diagnoser", "paid_intent", source="issue", count=1)
    summary = telemetry.summarize()
    assert summary.counts == {
        "impression": 10,
        "artifact_download": 5,
        "feedback_signal": 2,
        "paid_intent": 1,
    }
    assert summary.conversion["download_rate"] == pytest.approx(0.5)
    assert summary.conversion["feedback_rate"] == pytest.approx(0.4)
    assert summary.conversion["intent_rate"] == pytest.approx(0.5)
    assert summary.total_events == 4
    assert summary.invalid_events == 0
    assert set(STAGES) == set(summary.counts)


def test_funnel_counts_feedback_and_invalid_lines(tmp_path: Path) -> None:
    telemetry = FunnelTelemetry(tmp_path, clock=FrozenClock())
    telemetry.record("ci-failure-diagnoser", "impression", source="site", count=4)
    with telemetry.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"stage": "bogus", "count": 1}) + "\n")
        handle.write(json.dumps(["not", "an", "event"]) + "\n")
    summary = telemetry.summarize([make_feedback_item()])
    assert summary.counts["impression"] == 4
    assert summary.counts["feedback_signal"] == 1
    assert summary.invalid_events == 2

    with telemetry.path.open("a", encoding="utf-8") as handle:
        handle.write("{not json}\n")
    with pytest.raises(StateError):
        telemetry.summarize()


def test_funnel_rejects_invalid_input(tmp_path: Path) -> None:
    telemetry = FunnelTelemetry(tmp_path)
    with pytest.raises(StateError):
        telemetry.record("ci-failure-diagnoser", "unknown-stage", source="site")
    with pytest.raises(StateError):
        telemetry.record("../escape", "impression", source="site")
    with pytest.raises(StateError):
        telemetry.record("ci-failure-diagnoser", "impression", source="site", count=0)
    with pytest.raises(StateError):
        telemetry.record("ci-failure-diagnoser", "impression", source="site", count=True)


def test_funnel_dry_run_writes_nothing(tmp_path: Path) -> None:
    telemetry = FunnelTelemetry(tmp_path, dry_run=True)
    event = telemetry.record("ci-failure-diagnoser", "impression", source="site")
    assert event.stage == "impression"
    assert not telemetry.path.exists()


def test_build_result_json(builder: ProductBuilder, config: SystemConfig) -> None:
    build: BuildResult = builder.build(config.niche_by_id("ci-failure-diagnoser"))
    payload = build.to_json()
    assert payload["digest"] == build.digest
    assert payload["files"] == list(build.files)
    assert payload["feedback_count"] == 0
