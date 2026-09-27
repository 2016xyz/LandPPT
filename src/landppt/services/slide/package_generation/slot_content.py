"""Write into a selected layout's fields instead of asking for a generic page."""

import re

from ...template_package.schemas import PageContent
from .capacity import slot_limits

_COLLECTION = re.compile(r"^(blocks|metrics)\[(\d+)\]\.(\w+)$")


def slot_request(component):
    limits = slot_limits(component)
    return {
        "layout": component.id,
        "fields": {
            slot.field: {
                "required": slot.required,
                "max_chars": limits.get(slot.field, slot.max_chars),
            }
            for slot in component.slots
        },
        "blocks": component.blocks.model_dump(),
        "metrics": component.metrics.model_dump(),
    }


def content_from_fields(raw, component):
    """Reject omissions/extras; never drop, merge or fabricate the model's copy."""
    fields = raw.get("fields")
    if not isinstance(fields, dict):
        raise ValueError("fields 必须是所选版式的槽位文字对象")
    allowed = {slot.field: slot for slot in component.slots}
    unknown = fields.keys() - allowed.keys()
    if unknown:
        raise ValueError("所选版式没有这些槽位：" + ", ".join(sorted(unknown)))
    limits = slot_limits(component)
    for name, slot in allowed.items():
        value = fields.get(name, "")
        if not isinstance(value, str):
            raise ValueError(f"{name} 必须是文字")
        if slot.required and not value.strip():
            raise ValueError(f"缺少必填槽位 {name}")
        limit = limits.get(name, slot.max_chars)
        if len(value) > limit:
            raise ValueError(f"{name} 最多 {limit} 字，当前 {len(value)} 字")
    refs = raw.get("source_refs", [])
    payload = {"slide_id": raw["slide_id"], "source_refs": refs}
    collections = {"blocks": {}, "metrics": {}}
    for name, value in fields.items():
        if not value.strip():
            continue
        match = _COLLECTION.fullmatch(name)
        if match:
            collection, index, attribute = match.groups()
            index = int(index)
            item = collections[collection].setdefault(
                index, {"id": f"{collection}-{index}", "source_refs": refs}
            )
            item[attribute] = value
        else:
            payload[name] = value
    for name, items in collections.items():
        if items and sorted(items) != list(range(max(items) + 1)):
            raise ValueError(f"{name} 槽位必须从 0 连续填写，不能跳过要点")
        payload[name] = [items[index] for index in sorted(items)]
    return PageContent.model_validate(payload)
