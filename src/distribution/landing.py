"""Static, tracker-free landing pages for zero-cost distribution."""

from __future__ import annotations

import html
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.core.config import MonetizationChannel, NicheConfig
from src.core.io import ensure_dir, sha256_hex, write_json_atomic, write_text_atomic
from src.core.paths import RepoPaths
from src.core.timeutil import iso_now, utc_now
from src.core.types import JsonObject
from src.factory.builder import BuildResult

_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; "
    "base-uri 'none'; form-action 'none'"
)
_STYLE = """\
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  background: #0b1020; color: #e6ecff; line-height: 1.6;
}
main { max-width: 52rem; margin: 0 auto; padding: 4rem 1.5rem; }
.badge {
  display: inline-block; padding: .25rem .6rem; border: 1px solid #2d3b66;
  border-radius: 999px; color: #9fb3ff; font-size: .8rem; letter-spacing: .04em;
}
h1 { font-size: 2.4rem; line-height: 1.15; margin: 1.5rem 0 .5rem; }
h2 { margin-top: 2.5rem; font-size: 1.25rem; }
.lede { color: #b8c4e6; font-size: 1.1rem; }
ul { padding-left: 1.2rem; }
li { margin: .4rem 0; }
pre {
  background: #121a33; border: 1px solid #24325c; border-radius: .6rem;
  padding: 1rem; overflow-x: auto;
}
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.muted { color: #8d9ac1; font-size: .9rem; }
a { color: #8fb0ff; }
footer { margin-top: 3rem; border-top: 1px solid #24325c; padding-top: 1rem; }
"""


@dataclass(frozen=True)
class LandingPage:
    niche_id: str
    path: Path
    page_display: str
    sha256: str
    size_bytes: int
    channel_count: int
    created_at: str

    def to_json(self) -> JsonObject:
        return {
            "niche_id": self.niche_id,
            "path": self.page_display,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "channel_count": self.channel_count,
            "created_at": self.created_at,
        }


class LandingPageBuilder:
    def __init__(
        self,
        paths: RepoPaths,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._paths = paths
        self._clock = clock

    def render(
        self,
        build: BuildResult,
        niche: NicheConfig,
        channels: Sequence[MonetizationChannel],
        *,
        archive_name: str | None = None,
    ) -> LandingPage:
        site_dir = ensure_dir(self._paths.dist_dir / "site" / build.niche_id)
        page_path = site_dir / "index.html"
        page = self._html(build, niche, channels, archive_name)
        write_text_atomic(page_path, page)
        created_at = iso_now(self._clock())
        result = LandingPage(
            niche_id=niche.id,
            path=page_path,
            page_display=self._paths.display(page_path),
            sha256=sha256_hex(page),
            size_bytes=len(page.encode("utf-8")),
            channel_count=len(channels),
            created_at=created_at,
        )
        write_json_atomic(site_dir / "page.json", result.to_json())
        return result

    def _html(
        self,
        build: BuildResult,
        niche: NicheConfig,
        channels: Sequence[MonetizationChannel],
        archive_name: str | None,
    ) -> str:
        title = html.escape(build.product_name)
        description = html.escape(niche.description)
        features = "\n".join(f"      <li>{html.escape(feature)}</li>" for feature in build.features)
        archive = html.escape(archive_name or f"{build.product_name}-{build.version}.zip")
        support = self._support(channels)
        return f"""\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="{_CSP}">
  <meta name="description" content="{description}">
  <title>{title}</title>
  <style>
{_STYLE}  </style>
</head>
<body>
<main>
  <span class="badge">OPEN CORE · MIT · ZERO-COST</span>
  <h1>{title}</h1>
  <p class="lede">{description}</p>
  <section>
    <h2>What it does</h2>
    <ul>
{features}
    </ul>
  </section>
  <section>
    <h2>Get started</h2>
    <pre><code>python -m pip install {title}-{build.version}.zip
{title} --help</code></pre>
    <p class="muted">Release archive: {archive}. The tool runs offline and makes no
    network calls.</p>
  </section>
  <section>
    <h2>Support this project</h2>
{support}
    <p class="muted">No payment is collected by this page, no automated payouts are
    enabled, and no payment credentials live in the repository.</p>
  </section>
  <footer class="muted">
    Generated locally by SASK_CORE_LOOP. No cookies, trackers, or third-party assets.
  </footer>
</main>
</body>
</html>
"""

    @staticmethod
    def _support(channels: Sequence[MonetizationChannel]) -> str:
        if not channels:
            return (
                "    <p>Support channels are not configured yet. "
                "Star the repository and send sanitized feedback instead.</p>"
            )
        lines: list[str] = ["    <ul>"]
        for channel in channels:
            label = html.escape(channel.label)
            if channel.status == "active" and channel.url.startswith("https://"):
                url = html.escape(channel.url, quote=True)
                lines.append(
                    f'      <li><a href="{url}" rel="noopener noreferrer">{label}</a></li>'
                )
            elif channel.status == "disabled":
                lines.append(f"      <li>{label}: disabled</li>")
            else:
                lines.append(f"      <li>{label}: planned (human setup required)</li>")
        lines.append("    </ul>")
        return "\n".join(lines)
