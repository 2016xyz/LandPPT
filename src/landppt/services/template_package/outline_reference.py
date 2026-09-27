"""Turn a chosen package into outline constraints, so outlines fit its layouts.

Outlines written without the package in mind routinely ask for more points, or
longer points, than any component holds; the fast-mode writer then fails whole
pages. The reference is stored with the confirmed requirements at confirmation
time and appended to every outline prompt of the project.
"""

from __future__ import annotations

import re

from .schemas import TemplatePackage

REFERENCE_KEY = "package_outline_reference"
PREFERENCE_KEY = "generation_preference"

FAMILY_LABELS = {
    "cover": "封面",
    "section": "章节过渡",
    "points": "要点",
    "comparison": "对比",
    "process": "流程",
    "image": "图文",
    "metrics": "指标",
    "summary": "总结",
}
_BLOCK_FIELD = re.compile(r"^blocks\[(\d+)\]\.(heading|body)$")


async def load_package_preference(
    user_id: int, version_id: int, *, catalog_factory=None
):
    """Validate the user's version before either creation flow persists a project."""
    from .catalog import PackageCatalog, PackageNotFound

    if not version_id:
        raise ValueError("快速模式需要选择一个已发布的模板包")
    try:
        row = await (catalog_factory or PackageCatalog)(user_id).get(version_id)
    except PackageNotFound as exc:
        raise ValueError("所选模板包不存在或无权使用") from exc
    if row["status"] != "published":
        raise ValueError("只能选择已发布的模板包版本")
    package = TemplatePackage.model_validate(row["manifest"])
    return {
        "mode": "package",
        "version_id": row["id"],
        "name": row["template_name"],
    }, build_outline_reference(package)


def _field_summary(component) -> str:
    """Which content fields the layout actually shows; unbound ones are lost."""
    fields = {slot.field for slot in component.slots}
    parts = []
    if "subtitle" in fields:
        parts.append("副标题")
    kinds = {m.group(2) for f in fields if (m := _BLOCK_FIELD.match(f))}
    if kinds == {"heading", "body"}:
        parts.append("要点=小标题+说明")
    elif kinds == {"heading"}:
        parts.append("要点只有短标题（无说明）")
    elif kinds == {"body"}:
        parts.append("要点只有一句正文（无小标题）")
    if "takeaway" in fields:
        parts.append("结论句")
    if not parts:
        return "只显示标题"
    return "显示：标题、" + "、".join(parts)


def _component_line(component) -> str:
    from ..slide.package_generation.capacity import slot_limits

    limits = slot_limits(component)
    parts = []
    if "title" in limits:
        parts.append(f"标题≤{limits['title']}字")
    if "subtitle" in limits:
        parts.append(f"副标题≤{limits['subtitle']}字")
    blocks = component.blocks
    if blocks.maximum:
        count = (
            f"{blocks.minimum}"
            if blocks.minimum == blocks.maximum
            else f"{blocks.minimum}–{blocks.maximum}"
        )
        # The smallest block slot is what every point must survive.
        budgets = [n for f, n in limits.items() if _BLOCK_FIELD.match(f)]
        text = f"{count} 个要点"
        if budgets:
            text += f"（每个≤{min(budgets)}字）"
        parts.append(text)
    else:
        parts.append("无要点")
    if component.metrics.maximum:
        parts.append(
            f"{component.metrics.minimum}–{component.metrics.maximum} 个数据指标"
        )
    if component.images.maximum:
        parts.append(f"最多 {component.images.maximum} 张配图")
    family = FAMILY_LABELS.get(component.family, component.family)
    description = " ".join(component.description.split())[:80]
    return (
        f"- layout=\"{component.id}\"（{family}）：{'，'.join(parts)}；"
        f"{_field_summary(component)}。用途：{description}。逐槽位约束："
        + "，".join(
            f"{s.field}≤{limits.get(s.field, s.max_chars)}字"
            + ("（必填）" if s.required else "（可选）")
            for s in component.slots
            if s.kind == "text"
        )
    )


def build_outline_reference(package: TemplatePackage) -> str:
    ids = "、".join(f'"{c.id}"' for c in package.components)
    lines = [
        f"【快速模式模板包参考：{package.name}，共 {len(package.components)} 个版式】",
        "本项目的每一页都会套入下列某个版式，版式以外的结构无法显示。完整版式清单：",
        *(_component_line(c) for c in package.components),
        "大纲要求（必须遵守）：",
        f'1. 每页必须增加字段 "layout"，值只能是 {ids} 之一；按页面用途为每页选定一个版式。',
        "2. 该页 content_points 的数量必须落在所选版式的要点数量范围内；"
        "“无要点”的版式 content_points 为空。内容多时拆成多页，不要超量。",
        "3. 每个要点的长度不超过所选版式的字数上限；标题不超过其标题字数。"
        "版式只有正文时不要写“小标题：说明”，只有短标题时每个要点只写短语。",
        "4. 封面、章节、总结等页面优先选对应用途的版式；相邻页面尽量不要重复同一版式。",
    ]
    return "\n".join(lines)


def with_package_reference(text: str | None, confirmed_requirements) -> str:
    """Append the stored reference; a no-op for freeform projects."""
    base = text or ""
    reference = ""
    if isinstance(confirmed_requirements, dict):
        reference = confirmed_requirements.get(REFERENCE_KEY) or ""
    if not reference or reference in base:
        return base
    return f"{base}\n\n{reference}" if base else reference
