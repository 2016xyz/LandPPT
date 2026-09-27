"""Validate package SVGs before accepting them as reusable components."""

from __future__ import annotations

import math

from lxml import etree

from ..slide.svg_page.constants import FONT_FLOOR, NON_RENDERING_CONTAINERS
from ..slide.svg_page.sanitize import local_name, parse_svg, sanitize_svg, serialize
from .schemas import PageComponent

# Only used while validating unbound image nodes. Never returned as a rendered page.
VALIDATION_IMAGE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGO4/"
    "/whAAVQAqiKp7hiAAAAAElFTkSuQmCC"
)


class PackageValidationError(ValueError):
    pass


def _number(node: etree._Element, attribute: str, *, positive: bool = False) -> float:
    try:
        value = float(node.attrib[attribute])
    except (KeyError, ValueError) as exc:
        raise PackageValidationError(f"{node.get('id')}: invalid {attribute}") from exc
    if not math.isfinite(value) or (positive and value <= 0):
        raise PackageValidationError(f"{node.get('id')}: invalid {attribute}")
    return value


def validate_component(component: PageComponent, assets=()) -> etree._Element:
    """Return a fresh tree, rejecting invalid packages rather than repairing them."""
    root = parse_svg(component.svg)
    if root.get("viewBox") != "0 0 1280 720":
        raise PackageValidationError("package components require viewBox 0 0 1280 720")
    nodes = {}
    for node in root.iter():
        node_id = node.get("id")
        if node_id:
            if node_id in nodes:
                raise PackageValidationError(f"duplicate SVG ID: {node_id}")
            nodes[node_id] = node

    image_ids = set()
    asset_urls = {asset.id: asset.url() for asset in assets}
    for slot in component.slots:
        node = nodes.get(slot.node_id)
        expected = "image" if slot.kind == "image" else "text"
        if node is None or local_name(node) != expected or len(node):
            raise PackageValidationError(
                f"{slot.node_id}: expected a leaf {expected} node"
            )
        for ancestor in (node, *node.iterancestors()):
            if local_name(ancestor) in NON_RENDERING_CONTAINERS:
                raise PackageValidationError(f"{slot.node_id}: slot is not visible")
            if any(
                ancestor.get(attr) is not None
                for attr in (
                    "transform",
                    "style",
                    "display",
                    "visibility",
                    "clip-path",
                    "mask",
                )
            ):
                raise PackageValidationError(
                    f"{slot.node_id}: slots require direct, visible geometry"
                )
            if any(
                ancestor.get(attr, "1") != "1" for attr in ("opacity", "fill-opacity")
            ):
                raise PackageValidationError(f"{slot.node_id}: slots must be opaque")
        x, y = _number(node, "x"), _number(node, "y")
        if slot.kind == "text":
            if node.get("data-align", "left") not in {"left", "center", "right"}:
                raise PackageValidationError("invalid horizontal alignment")
            if node.get("data-valign", "top") not in {"top", "middle", "bottom"}:
                raise PackageValidationError("invalid vertical alignment")
            width = _number(node, "data-box-w", positive=True)
            height = _number(node, "data-box-h", positive=True)
            size = _number(node, "font-size", positive=True)
            if (
                size < FONT_FLOOR
                or size > 160
                or node.get("fill") in ("none", "transparent")
            ):
                raise PackageValidationError(f"{slot.node_id}: invalid text style")
            if node.get("text-anchor", "start") != "start":
                raise PackageValidationError(
                    f"{slot.node_id}: slots require start alignment"
                )
            top = y - size
        else:
            if node.get("data-crop", "rect") not in {"rect", "circle", "rounded"}:
                raise PackageValidationError("unsupported image crop")
            width = _number(node, "width", positive=True)
            height = _number(node, "height", positive=True)
            top = y
            image_ids.add(slot.node_id)
            if any(etree.QName(attr).localname == "href" for attr in node.attrib):
                raise PackageValidationError(
                    "image sources must be supplied by the asset resolver"
                )
            node.set("href", VALIDATION_IMAGE)
        if x < 0 or top < 0 or x + width > 1280 or top + height > 720:
            raise PackageValidationError(
                f"{slot.node_id}: slot extends outside the canvas"
            )

    for node in root.iter():
        if local_name(node) == "image" and node.get("id") not in image_ids:
            asset_id = node.get("data-asset")
            if not asset_id or asset_id not in asset_urls:
                raise PackageValidationError(
                    "every image must have a declared slot or package asset"
                )
            if any(etree.QName(attr).localname == "href" for attr in node.attrib):
                raise PackageValidationError(
                    "fixed images must use data-asset, not href"
                )
            node.set("href", asset_urls[asset_id])
        elif node.get("data-asset") is not None:
            raise PackageValidationError("data-asset is only allowed on fixed images")
    sanitized = sanitize_svg(
        serialize(root), allowed_image_urls={VALIDATION_IMAGE, *asset_urls.values()}
    )
    if sanitized.issues:
        raise PackageValidationError(
            "unsafe or unsupported SVG: " + "; ".join(sanitized.issues)
        )
    return sanitized.root
