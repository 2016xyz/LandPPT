"""Bind text through XML nodes, resolve approved images, then measure the result."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import unquote, urlsplit

from ...template_package.schemas import PageContent, TemplatePackage
from ...template_package.validator import validate_component
from ..svg_page.inspect import inspect_svg
from ..svg_page.layout import layout_text_elements
from ..svg_page.metrics import get_text_measurer
from ..svg_page.sanitize import sanitize_svg, serialize
from ..svg_page.shell import build_slide_html
from .candidate_filter import display_fields, incompatibilities
from .capacity import slot_limits


class PackageRenderError(ValueError):
    def __init__(self, problems):
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class RenderedPage:
    html_content: str
    svg: str
    metadata: dict


def _safe_asset_url(url: str) -> bool:
    decoded = unquote(url)
    if any(ord(char) < 32 for char in decoded) or "\\" in decoded:
        return False
    if decoded.startswith("data:image/"):
        return any(
            decoded.startswith(f"data:image/{kind};base64,")
            for kind in ("png", "jpeg", "webp")
        )
    try:
        parts = urlsplit(decoded)
    except ValueError:
        return False
    if parts.username or parts.password or ".." in parts.path.split("/"):
        return False
    return bool(
        (parts.scheme in ("https", "http") and parts.netloc)
        or (
            not parts.scheme
            and not parts.netloc
            and decoded.startswith("/")
            and not decoded.startswith("//")
        )
    )


def render_page(
    package: TemplatePackage,
    component_id: str,
    content: PageContent,
    *,
    assets: Mapping[str, str] | None = None,
    allowed_image_urls: frozenset[str] = frozenset(),
) -> RenderedPage:
    """No model calls, truncation, placeholder success, or template mutation.

    The caller must resolve visual IDs to assets and supply its approved URL set.
    This function never downloads assets or grants access to a project.
    """
    component = next(
        (item for item in package.components if item.id == component_id), None
    )
    if component is None:
        raise PackageRenderError([f"unknown component: {component_id}"])
    problems = incompatibilities(component, content)
    if problems:
        raise PackageRenderError(problems)
    try:
        root = validate_component(component)
    except ValueError as exc:
        raise PackageRenderError([str(exc)]) from exc
    nodes = {node.get("id"): node for node in root.iter() if node.get("id")}
    fields = display_fields(content)
    assets = assets or {}
    used_urls = set()
    for slot in component.slots:
        node = nodes[slot.node_id]
        value = fields.get(slot.field, "")
        if slot.kind == "text":
            node.text = value
        elif not value:
            node.getparent().remove(node)
        else:
            url = assets.get(value, "")
            if not url or url not in allowed_image_urls or not _safe_asset_url(url):
                raise PackageRenderError([f"missing or unapproved image: {value}"])
            node.set("href", url)
            node.set("preserveAspectRatio", "xMidYMid slice")
            used_urls.add(url)
    sanitized = sanitize_svg(serialize(root), allowed_image_urls=used_urls)
    if sanitized.issues:
        raise PackageRenderError(sanitized.issues)
    measurer = get_text_measurer()
    layouts = layout_text_elements(sanitized.root, measurer)
    inspection = inspect_svg(
        sanitized.root, measurer, layouts, sanitized.hand_path_count
    )
    problems = []
    fields_by_node = {slot.node_id: slot.field for slot in component.slots}
    limits = slot_limits(component)

    def describe(node_id):
        # Repairs need the content field and a budget, not an SVG node ID.
        field = fields_by_node.get(node_id)
        if not field:
            return node_id
        return (
            f"{node_id} ({field}: {len(fields.get(field, ''))} 字，"
            f"此版式建议不超过 {limits.get(field)} 字)"
        )

    for defect in inspection.blocking:
        node_id = defect.get("id") or defect.get("a") or ""
        problems.append(f"layout: {describe(node_id)} {defect}")
    for layout in layouts:
        if layout.font_size < max(18, layout.original_font_size * 0.75):
            problems.append(
                f"excessive font reduction: {describe(layout.element_id)}"
            )
    if problems:
        raise PackageRenderError(problems)
    markup = sanitized.markup
    return RenderedPage(
        html_content=build_slide_html(
            markup, lang=package.language, title=content.title
        ),
        svg=markup,
        metadata={
            "generation_mode": "package",
            "render_mode": "svg",
            "package_id": package.package_id,
            "package_version": package.version,
            "package_hash": package.content_hash(),
            "component_id": component.id,
            "slide_id": content.slide_id,
            "content_revision": content.content_revision,
            "bindings": {slot.node_id: slot.field for slot in component.slots},
            "assets": {
                visual.id: assets[visual.id] for visual in content.visual_briefs
            },
            "layout_report": {"blocking": [], "advisory": inspection.advisory},
        },
    )
