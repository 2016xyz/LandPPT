from unittest.mock import AsyncMock

import pytest

from landppt.services.slide.package_generation.content_service import ContentService
from landppt.services.slide.package_generation.renderer import render_page
from landppt.services.slide.package_generation.slot_content import content_from_fields
from landppt.services.slide.svg_page.sanitize import parse_svg, serialize
from landppt.services.template_package.builtin import load_builtin_package
from landppt.services.template_package.outline_reference import build_outline_reference


def mixed_package():
    package = load_builtin_package()
    component = next(c for c in package.components if c.id == "points_3")
    removed = next(s for s in component.slots if s.field == "blocks[0].heading")
    root = parse_svg(component.svg)
    for node in root.iter():
        if node.get("id") == removed.node_id:
            node.getparent().remove(node)
            break
    component = component.model_copy(
        update={
            "slots": tuple(s for s in component.slots if s is not removed),
            "svg": serialize(root),
        }
    )
    return package.model_copy(update={"components": (component,)})


def reply(component):
    return {
        "slide_id": "page1",
        "fields": {s.field: "新内容" for s in component.slots},
        "source_refs": ["outline"],
    }


@pytest.mark.asyncio
async def test_mixed_layout_generates_only_bound_fields_and_renders():
    package = mixed_package()
    component = package.components[0]
    service = ContentService(None)
    service.json_completion = AsyncMock(
        return_value=({"pages": [reply(component)]}, {})
    )
    valid, errors, _ = await service.expand(
        {"outline": {}}, package, [{"slide_id": "page1", "layout": component.id}]
    )
    assert not errors
    content = valid["page1"]
    assert content.blocks[0].heading == ""
    assert content.blocks[1].heading == "新内容"
    assert "新内容" in render_page(package, component.id, content).svg
    prompt = service.json_completion.call_args.args[0]
    assert '"write_into"' in prompt
    assert '"blocks[0].heading"' not in prompt
    assert "blocks[1].heading" in build_outline_reference(package)


@pytest.mark.parametrize("defect", ["extra", "missing", "too_long"])
def test_slot_writer_rejects_invalid_copy_without_dropping_it(defect):
    component = mixed_package().components[0]
    raw = reply(component)
    if defect == "extra":
        raw["fields"]["blocks[0].heading"] = "多余标题"
    elif defect == "missing":
        raw["fields"].pop("title")
    else:
        raw["fields"]["title"] = "字" * 1000
    with pytest.raises(ValueError):
        content_from_fields(raw, component)
