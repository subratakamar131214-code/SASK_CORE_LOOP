"""Release manifests and optional human-triggered GitHub release publication."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from src.core.errors import StateError
from src.core.io import (
    ensure_dir,
    read_json,
    sha256_hex,
    write_json_atomic,
)
from src.core.paths import RepoPaths
from src.core.redact import sanitize_public_payload
from src.core.timeutil import iso_now, utc_now
from src.core.types import JsonObject, JsonValue
from src.distribution.landing import LandingPage
from src.distribution.packager import PackageArtifact
from src.factory.builder import BuildResult
from src.ledger.ledger import Ledger
from src.ops.gh import GitHubClient, RepoRef

_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_RELEASE_ID = re.compile(r"^rel-[0-9a-f]{12}$")


@dataclass(frozen=True)
class ReleaseManifest:
    release_id: str
    niche_id: str
    product_name: str
    version: str
    tag: str
    build_digest: str
    archive: str
    archive_sha256: str
    landing_page: str
    notes: str
    status: str
    created_at: str
    published_url: str | None = None

    def to_json(self) -> JsonObject:
        return {
            "release_id": self.release_id,
            "niche_id": self.niche_id,
            "product_name": self.product_name,
            "version": self.version,
            "tag": self.tag,
            "build_digest": self.build_digest,
            "archive": self.archive,
            "archive_sha256": self.archive_sha256,
            "landing_page": self.landing_page,
            "notes": self.notes,
            "status": self.status,
            "created_at": self.created_at,
            "published_url": self.published_url,
        }

    @classmethod
    def from_json(cls, value: JsonValue) -> ReleaseManifest:
        if not isinstance(value, dict):
            raise StateError("release manifest is not an object")
        fields = (
            "release_id",
            "niche_id",
            "product_name",
            "version",
            "tag",
            "build_digest",
            "archive",
            "archive_sha256",
            "landing_page",
            "notes",
            "status",
            "created_at",
        )
        data: dict[str, str] = {}
        for field in fields:
            item = value.get(field)
            if not isinstance(item, str):
                raise StateError(f"release manifest field {field} is missing")
            data[field] = item
        published = value.get("published_url")
        if published is not None and not isinstance(published, str):
            raise StateError("release manifest published_url is invalid")
        return cls(**data, published_url=published)


class ReleaseManager:
    def __init__(
        self,
        paths: RepoPaths,
        ledger: Ledger,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._paths = paths
        self._ledger = ledger
        self._clock = clock

    def prepare(
        self,
        build: BuildResult,
        package: PackageArtifact,
        landing: LandingPage,
        *,
        notes: str = "",
    ) -> ReleaseManifest:
        ensure_dir(self._paths.releases_dir)
        tag = f"v{build.version}-{build.niche_id}"
        if not _TAG.fullmatch(tag):
            raise StateError(f"unsafe release tag: {tag!r}")
        release_id = "rel-" + sha256_hex(
            f"{build.niche_id}|{build.version}|{package.sha256}"
        )[:12]
        path = self._paths.releases_dir / f"{release_id}.json"
        if path.exists():
            return ReleaseManifest.from_json(read_json(path, None))
        sanitized_notes, _ = sanitize_public_payload({"notes": notes[:2000]})
        note_text = sanitized_notes["notes"] if isinstance(sanitized_notes, dict) else ""
        manifest = ReleaseManifest(
            release_id=release_id,
            niche_id=build.niche_id,
            product_name=build.product_name,
            version=build.version,
            tag=tag,
            build_digest=build.digest,
            archive=package.archive_display,
            archive_sha256=package.sha256,
            landing_page=landing.page_display,
            notes=note_text if isinstance(note_text, str) else "",
            status="prepared",
            created_at=iso_now(self._clock()),
        )
        write_json_atomic(path, manifest.to_json())
        self._ledger.append(
            "release",
            {
                "release_id": release_id,
                "niche_id": build.niche_id,
                "tag": tag,
                "archive_sha256": package.sha256,
                "build_digest": build.digest,
                "status": "prepared",
                "hard_cap_usd": 0.0,
            },
        )
        return manifest

    def publish(
        self,
        manifest: ReleaseManifest,
        client: GitHubClient,
        repo: RepoRef,
    ) -> ReleaseManifest:
        if not _RELEASE_ID.fullmatch(manifest.release_id):
            raise StateError("release id is invalid")
        path = self._paths.releases_dir / f"{manifest.release_id}.json"
        if not path.exists():
            raise StateError(f"release manifest is missing: {manifest.release_id}")
        archive = (self._paths.root / manifest.archive).resolve()
        if self._paths.dist_dir.resolve() not in archive.parents:
            raise StateError("release archive is outside the distribution directory")
        url = client.create_release(
            repo,
            tag=manifest.tag,
            title=f"{manifest.product_name} {manifest.version}",
            notes=manifest.notes or "Open-core release.",
            assets=[archive] if archive.is_file() else [],
        )
        updated = replace(manifest, status="published", published_url=url)
        write_json_atomic(path, updated.to_json())
        self._ledger.append(
            "release",
            {
                "release_id": manifest.release_id,
                "niche_id": manifest.niche_id,
                "tag": manifest.tag,
                "status": "published",
                "url": url,
                "hard_cap_usd": 0.0,
            },
        )
        return updated
