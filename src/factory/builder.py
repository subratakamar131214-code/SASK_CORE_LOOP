"""Assemble open-core products from deterministic templates."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.core.config import NicheConfig
from src.core.errors import StateError, VerificationError
from src.core.io import (
    canonical_json,
    ensure_dir,
    sha256_hex,
    write_json_atomic,
    write_text_atomic,
)
from src.core.paths import RepoPaths
from src.core.timeutil import iso_now, utc_now
from src.core.types import JsonObject, JsonValue
from src.factory.docsgen import generate_docs
from src.factory.spec import ProductSpec
from src.factory.templates import render_template_files
from src.factory.testgen import generate_tests
from src.sensors.feedback import FeedbackItem
from src.sensors.sanitizer import sanitize_feedback_text

_FEATURES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "ci_failure_diagnoser": (
        (
            "Classifies failed CI logs into seven deterministic categories.",
            "Surfaces evidence lines and remediation hints.",
            "Emits JSON for CI gates and automation.",
            "Runs offline with no third-party runtime dependencies.",
        ),
        (
            "Cross-job timeline correlation (human-gated).",
            "Organization policy packs (human-gated).",
            "Hosted dashboard with OAuth (human-gated).",
        ),
    ),
    "openapi_schema_migrator": (
        (
            "Detects removed operations and parameters.",
            "Flags parameter type changes and new required request fields.",
            "Reports removed response fields for consumers.",
            "Produces Markdown or JSON reports with CI exit codes.",
        ),
        (
            "Schema evolution suggestions (human-gated).",
            "Consumer impact graph (human-gated).",
            "Hosted history and approval workflow (human-gated).",
        ),
    ),
}


@dataclass(frozen=True)
class BuildResult:
    niche_id: str
    product_name: str
    package_name: str
    product_type: str
    version: str
    root: Path
    root_display: str
    files: tuple[str, ...]
    source_files: tuple[str, ...]
    test_files: tuple[str, ...]
    doc_files: tuple[str, ...]
    features: tuple[str, ...]
    digest: str
    feedback_count: int

    def to_json(self) -> JsonObject:
        return {
            "niche_id": self.niche_id,
            "product_name": self.product_name,
            "package_name": self.package_name,
            "product_type": self.product_type,
            "version": self.version,
            "root": self.root_display,
            "files": list(self.files),
            "features": list(self.features),
            "digest": self.digest,
            "feedback_count": self.feedback_count,
        }


class ProductBuilder:
    def __init__(
        self,
        paths: RepoPaths,
        *,
        version: str = "0.1.0",
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._paths = paths
        self._version = version
        self._clock = clock

    def build(
        self,
        niche: NicheConfig,
        feedback: Sequence[FeedbackItem] = (),
    ) -> BuildResult:
        features, pro_features = _FEATURES.get(
            niche.product_type,
            (("Deterministic core workflow.",), ("Advanced automation (human-gated).",)),
        )
        feedback_titles = _prioritized_titles(niche, feedback)
        spec = ProductSpec.create(
            niche_id=niche.id,
            product_name=niche.product_name,
            product_type=niche.product_type,
            description=niche.description,
            version=self._version,
            features=features,
            pro_features=pro_features,
            feedback_titles=feedback_titles,
        )
        rendered = render_template_files(spec)
        rendered.update(generate_tests(spec))
        rendered.update(generate_docs(spec))

        root = self._paths.build_dir / niche.id
        self._prepare_root(root)
        checksums: dict[str, JsonValue] = {}
        for relative, text in sorted(rendered.items()):
            target = self._safe_target(root, relative)
            if target.suffix == ".py":
                try:
                    compile(text, str(target), "exec")
                except SyntaxError as exc:
                    raise VerificationError(
                        f"generated file has invalid syntax: {relative}"
                    ) from exc
            write_text_atomic(target, text)
            checksums[relative] = sha256_hex(text)

        digest = sha256_hex(canonical_json(checksums))
        source_files = tuple(
            path for path in rendered if path.startswith("src/") and path.endswith(".py")
        )
        test_files = tuple(path for path in rendered if path.startswith("tests/"))
        doc_files = tuple(
            path for path in rendered if path.endswith(".md") or path == "LICENSE"
        )
        manifest: JsonObject = {
            "product": spec.to_json(),
            "generated_at": iso_now(self._clock()),
            "digest": digest,
            "files": checksums,
        }
        write_json_atomic(root / "sask-build.json", manifest)
        return BuildResult(
            niche_id=niche.id,
            product_name=spec.product_name,
            package_name=spec.package_name,
            product_type=spec.product_type,
            version=spec.version,
            root=root,
            root_display=self._paths.display(root),
            files=tuple(sorted(rendered)),
            source_files=tuple(sorted(source_files)),
            test_files=tuple(sorted(test_files)),
            doc_files=tuple(sorted(doc_files)),
            features=features,
            digest=digest,
            feedback_count=len(feedback_titles),
        )

    def _prepare_root(self, root: Path) -> None:
        ensure_dir(self._paths.build_dir)
        if root.exists():
            if not root.is_dir() or root.parent.resolve() != self._paths.build_dir.resolve():
                raise StateError(f"refusing to clear unexpected build path: {root}")
            shutil.rmtree(root)
        ensure_dir(root)

    @staticmethod
    def _safe_target(root: Path, relative: str) -> Path:
        candidate = (root / relative).resolve()
        if root.resolve() not in candidate.parents:
            raise StateError(f"unsafe generated path: {relative!r}")
        return candidate


def _prioritized_titles(niche: NicheConfig, feedback: Sequence[FeedbackItem]) -> tuple[str, ...]:
    matched = [item for item in feedback if niche.id in item.matched_niches]
    ordered = sorted(matched, key=lambda item: (-item.priority, item.id))
    titles: list[str] = []
    for item in ordered:
        title = sanitize_feedback_text(item.title, max_chars=140)
        if title and title not in titles:
            titles.append(title)
        if len(titles) >= 5:
            break
    return tuple(titles)
