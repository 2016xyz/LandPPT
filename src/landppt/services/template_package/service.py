"""Offline package validation gate, reusable by future persistence and API layers."""

from __future__ import annotations

from .schemas import TemplatePackage
from .validator import VALIDATION_IMAGE, PackageValidationError, validate_component


def example_assets(package, component):
    urls = {asset.id: asset.url() for asset in package.assets}
    return {
        visual.id: urls.get(component.sample_assets.get(visual.id), VALIDATION_IMAGE)
        for visual in component.examples[0].visual_briefs
    }


#: A text slot smaller than this cannot hold generated copy; every page placed on
#: the layout would fail. Rejected at publish time instead of at generation time.
MIN_SLOT_CHARS = {"title": 6, "subtitle": 8, "heading": 2, "body": 8}


def capacity_problems(component) -> list[str]:
    from ..slide.package_generation.capacity import slot_limits

    problems = []
    for field, limit in slot_limits(component).items():
        kind = field.rsplit(".", 1)[-1] if field.startswith("blocks[") else field
        need = MIN_SLOT_CHARS.get(kind, 0)
        if limit < need:
            problems.append(f"{field} 只能容纳 {limit} 字（至少 {need} 字）")
    return problems


def validate_package(package: TemplatePackage) -> dict:
    """Check every component and render every example; no publishing side effects."""
    from ..slide.package_generation.renderer import render_page

    rendered = 0
    for component in package.components:
        validate_component(component, package.assets)
        problems = capacity_problems(component)
        if problems:
            raise PackageValidationError(
                f"{component.id}: 文字区域太小，" + "；".join(problems)
            )
        for example in component.examples:
            try:
                render_page(
                    package,
                    component.id,
                    example,
                    assets={
                        visual.id: VALIDATION_IMAGE for visual in example.visual_briefs
                    },
                    allowed_image_urls=frozenset({VALIDATION_IMAGE}),
                )
            except ValueError as exc:
                raise PackageValidationError(
                    f"{component.id}/{example.slide_id}: {exc}"
                ) from exc
            rendered += 1
    return {
        "package_id": package.package_id,
        "version": package.version,
        "content_hash": package.content_hash(),
        "components": len(package.components),
        "examples_rendered": rendered,
    }
