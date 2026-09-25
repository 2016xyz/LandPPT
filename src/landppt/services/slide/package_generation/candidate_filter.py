"""Hard constraints used before any semantic selector, including Jev."""

from __future__ import annotations

from ...template_package.schemas import PageComponent, PageContent, TemplatePackage


def display_fields(content: PageContent) -> dict[str, str]:
    """Enumerate final copy without evaluating arbitrary field paths."""
    fields = {key: getattr(content, key) for key in ("title", "subtitle", "takeaway")}
    for collection, attributes in (
        ("blocks", ("heading", "body")),
        ("metrics", ("label", "value", "unit")),
        ("visual_briefs", ("id",)),
    ):
        for index, item in enumerate(getattr(content, collection)):
            for attribute in attributes:
                fields[f"{collection}[{index}].{attribute}"] = getattr(item, attribute)
    return {key: value for key, value in fields.items() if value}


def incompatibilities(
    component: PageComponent,
    content: PageContent,
    *,
    allow_images: bool = True,
    image_budget: int | None = None,
) -> tuple[str, ...]:
    problems = []
    for name, items in (
        ("blocks", content.blocks),
        ("metrics", content.metrics),
        ("images", content.visual_briefs),
    ):
        capacity = getattr(component, name)
        if not capacity.minimum <= len(items) <= capacity.maximum:
            problems.append(
                f"{name}: requires {capacity.minimum}..{capacity.maximum} items, "
                f"got {len(items)}"
            )
    image_count = len(content.visual_briefs)
    if not allow_images and (image_count or component.images.minimum):
        problems.append("images are disabled")
    if image_budget is not None and image_count > image_budget:
        problems.append("image budget exceeded")
    fields = display_fields(content)
    bound = {slot.field for slot in component.slots}
    for field in sorted(fields.keys() - bound):
        problems.append(f"unbound content: {field}")
    for slot in component.slots:
        value = fields.get(slot.field, "")
        if slot.required and not value:
            problems.append(f"missing required content: {slot.field}")
        if slot.kind == "text" and len(value) > slot.max_chars:
            problems.append(
                f"capacity exceeded: {slot.field} ({len(value)} > {slot.max_chars})"
            )
    return tuple(problems)


def structural_problems(component, content, **kwargs) -> tuple[str, ...]:
    """Defects a text rewrite inside the same structure cannot fix."""
    return tuple(
        p
        for p in incompatibilities(component, content, **kwargs)
        if not p.startswith("capacity exceeded")
    )


def has_structural_fit(package, content, **kwargs) -> bool:
    return any(not structural_problems(c, content, **kwargs) for c in package.components)


def all_problems(package, content, **kwargs) -> dict[str, list[str]]:
    """Per-component reasons, so a repair knows what shape would fit."""
    return {
        c.id: list(incompatibilities(c, content, **kwargs)) for c in package.components
    }


def demote_metrics(content: PageContent, package, **kwargs) -> PageContent | None:
    """Keep every metric as text when no layout holds this blocks/metrics mix.

    Tries metrics as extra blocks, then metrics merged into existing block bodies;
    returns the first shape some component accepts. No fact is dropped.
    """
    if not content.metrics:
        return None
    base = content.model_dump(mode="json")
    texts = [f"{m.label}：{m.value}{m.unit}" for m in content.metrics]
    shapes = []
    if len(content.blocks) + len(content.metrics) <= 20:
        taken = {b.id for b in content.blocks}
        extra = []
        for metric, text in zip(content.metrics, texts):
            block_id = f"m-{metric.id}"[:100]
            while block_id in taken:
                block_id = (block_id + "x")[-100:]
            taken.add(block_id)
            extra.append(
                {
                    "id": block_id,
                    "kind": "point",
                    "heading": metric.label,
                    "body": f"{metric.value}{metric.unit}",
                    "source_refs": list(metric.source_refs),
                }
            )
        shapes.append([*base["blocks"], *extra])
    if content.blocks:
        merged = [dict(b, source_refs=list(b["source_refs"])) for b in base["blocks"]]
        for i, (metric, text) in enumerate(zip(content.metrics, texts)):
            block = merged[i % len(merged)]
            block["body"] = "；".join(p for p in (block["body"], text) if p)
            block["source_refs"] = sorted(
                set(block["source_refs"]) | set(metric.source_refs)
            )
        shapes.append(merged)
    for blocks in shapes:
        candidate = PageContent.model_validate({**base, "blocks": blocks, "metrics": []})
        if has_structural_fit(package, candidate, **kwargs):
            return candidate
    return None


def compatible_components(
    package: TemplatePackage,
    content: PageContent,
    *,
    allow_images: bool = True,
    image_budget: int | None = None,
    locked_component_id: str | None = None,
) -> tuple[PageComponent, ...]:
    return tuple(
        component
        for component in package.components
        if (locked_component_id is None or component.id == locked_component_id)
        and not incompatibilities(
            component, content, allow_images=allow_images, image_budget=image_budget
        )
    )
