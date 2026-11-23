"""Build deterministic, license-checked, secret-free distribution archives."""

from __future__ import annotations

import os
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.compliance.policy import LicenseChecker
from src.compliance.secretscan import assert_clean, scan_text
from src.core.config import ComplianceConfig
from src.core.errors import ComplianceError, StateError
from src.core.io import ensure_dir, sha256_file, write_json_atomic, write_text_atomic
from src.core.paths import RepoPaths
from src.core.timeutil import iso_now, utc_now
from src.core.types import JsonObject
from src.factory.builder import BuildResult

_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True)
class PackageArtifact:
    niche_id: str
    product_name: str
    version: str
    archive: Path
    archive_display: str
    sha256: str
    size_bytes: int
    file_count: int
    license_id: str
    created_at: str

    def to_json(self) -> JsonObject:
        return {
            "niche_id": self.niche_id,
            "product_name": self.product_name,
            "version": self.version,
            "archive": self.archive_display,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "file_count": self.file_count,
            "license": self.license_id,
            "created_at": self.created_at,
        }


class PackageBuilder:
    def __init__(
        self,
        paths: RepoPaths,
        compliance: ComplianceConfig,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._paths = paths
        self._compliance = compliance
        self._licenses = LicenseChecker(compliance.allowed_licenses)
        self._clock = clock

    def package(self, build: BuildResult, *, license_id: str = "MIT") -> PackageArtifact:
        self._licenses.assert_allowed(license_id, subject=build.product_name)
        if not build.root.is_dir():
            raise StateError(f"build output is missing: {build.root}")
        output_dir = ensure_dir(self._paths.dist_dir / build.niche_id)
        archive = output_dir / f"{build.product_name}-{build.version}.zip"
        prefix = f"{build.product_name}-{build.version}"
        files = self._collect(build)
        self._scan(build, files, license_id)

        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{archive.name}.", suffix=".tmp", dir=output_dir
        )
        os.close(descriptor)
        temp_archive = Path(temp_name)
        try:
            with zipfile.ZipFile(temp_archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for relative, path in files:
                    info = zipfile.ZipInfo(f"{prefix}/{relative}", date_time=_FIXED_ZIP_TIME)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o644 << 16
                    bundle.writestr(info, path.read_bytes())
            os.replace(temp_archive, archive)
        except OSError as exc:
            raise StateError(f"cannot create archive {archive}: {exc}") from exc
        finally:
            temp_archive.unlink(missing_ok=True)

        digest = sha256_file(archive)
        created_at = iso_now(self._clock())
        write_text_atomic(
            output_dir / "SHA256SUMS",
            f"{digest}  {archive.name}\n",
        )
        artifact = PackageArtifact(
            niche_id=build.niche_id,
            product_name=build.product_name,
            version=build.version,
            archive=archive,
            archive_display=self._paths.display(archive),
            sha256=digest,
            size_bytes=archive.stat().st_size,
            file_count=len(files),
            license_id=license_id,
            created_at=created_at,
        )
        write_json_atomic(output_dir / "package.json", artifact.to_json())
        return artifact

    def _collect(self, build: BuildResult) -> list[tuple[str, Path]]:
        files: list[tuple[str, Path]] = []
        for path in sorted(build.root.rglob("*")):
            if path.is_symlink():
                raise StateError(f"refusing to package symlink: {path}")
            if not path.is_file():
                continue
            relative = path.relative_to(build.root).as_posix()
            files.append((relative, path))
        if not files:
            raise StateError(f"no files to package in {build.root}")
        return files

    def _scan(
        self, build: BuildResult, files: list[tuple[str, Path]], license_id: str
    ) -> None:
        if not self._licenses.is_allowed(license_id):
            raise ComplianceError(f"license {license_id!r} is not allowed")
        findings = []
        for relative, path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            findings.extend(
                scan_text(text, path=f"{build.product_name}/{relative}")
            )
        assert_clean(findings)
