"""Offline package validation gate, reusable by future persistence and API layers."""

from __future__ import annotations

from .schemas import TemplatePackage
from .validator import VALIDATION_IMAGE, PackageValidationError, validate_component


def validate_package(package: TemplatePackage) -> dict:
    """Check every component and render every example; no publishing side effects."""
    from ..slide.package_generation.renderer import render_page

    rendered = 0
    for component in package.components:
        validate_component(component)
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
