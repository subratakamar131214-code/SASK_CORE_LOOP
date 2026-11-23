"""Product specification shared by the open-core build pipeline."""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.core.types import JsonObject

_SAFE_SLUG = re.compile(r"[^a-z0-9]+")


def package_name_for(product_name: str) -> str:
    slug = _SAFE_SLUG.sub("_", product_name.lower()).strip("_")
    if not slug or not slug[0].isalpha():
        slug = f"product_{slug}" if slug else "product"
    return slug


@dataclass(frozen=True)
class ProductSpec:
    niche_id: str
    product_name: str
    package_name: str
    product_type: str
    description: str
    version: str = "0.1.0"
    license_id: str = "MIT"
    features: tuple[str, ...] = ()
    pro_features: tuple[str, ...] = ()
    feedback_titles: tuple[str, ...] = ()
    workflow_bottleneck: str = ""
    budget_owner: str = ""
    reliability_rationale: str = ""

    @classmethod
    def create(
        cls,
        *,
        niche_id: str,
        product_name: str,
        product_type: str,
        description: str,
        version: str,
        features: tuple[str, ...],
        pro_features: tuple[str, ...],
        feedback_titles: tuple[str, ...] = (),
        workflow_bottleneck: str = "",
        budget_owner: str = "",
        reliability_rationale: str = "",
    ) -> ProductSpec:
        return cls(
            niche_id=niche_id,
            product_name=product_name,
            package_name=package_name_for(product_name),
            product_type=product_type,
            description=description,
            version=version,
            features=features,
            pro_features=pro_features,
            feedback_titles=feedback_titles,
            workflow_bottleneck=workflow_bottleneck,
            budget_owner=budget_owner,
            reliability_rationale=reliability_rationale,
        )

    def to_json(self) -> JsonObject:
        return {
            "niche_id": self.niche_id,
            "product_name": self.product_name,
            "package_name": self.package_name,
            "product_type": self.product_type,
            "version": self.version,
            "license": self.license_id,
            "feedback_titles": list(self.feedback_titles),
            "workflow_bottleneck": self.workflow_bottleneck,
            "budget_owner": self.budget_owner,
            "reliability_rationale": self.reliability_rationale,
        }
