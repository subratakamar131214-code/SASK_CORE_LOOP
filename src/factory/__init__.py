"""Zero-cost open-core product factory: build, tests, and documentation."""

from src.factory.builder import BuildResult, ProductBuilder
from src.factory.spec import ProductSpec, package_name_for

__all__ = ["BuildResult", "ProductBuilder", "ProductSpec", "package_name_for"]
