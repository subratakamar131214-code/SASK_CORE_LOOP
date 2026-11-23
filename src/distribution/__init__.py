"""Zero-cost distribution: packaging, landing pages, and privacy-safe telemetry."""

from src.distribution.funnel import STAGES, FunnelEvent, FunnelSummary, FunnelTelemetry
from src.distribution.landing import LandingPage, LandingPageBuilder
from src.distribution.packager import PackageArtifact, PackageBuilder

__all__ = [
    "STAGES",
    "FunnelEvent",
    "FunnelSummary",
    "FunnelTelemetry",
    "LandingPage",
    "LandingPageBuilder",
    "PackageArtifact",
    "PackageBuilder",
]
