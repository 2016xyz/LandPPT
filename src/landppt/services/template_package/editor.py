"""AI edits are validated patches applied to a draft; published versions never change."""

import asyncio
import json
from contextlib import aclosing
from typing import Literal

from pydantic import Field, model_validator

from ..slide.package_generation.content_service import ContentService
from .builtin import load_builtin_package
from .reply_stream import reply_completion, stream_replies
from .schemas import ContractModel, Identifier, PageComponent, TemplatePackage
from .service import example_assets, validate_package


class ComponentEdit(ContractModel):
    action: Literal["modify", "delete", "add"]
    component_id: Identifier
    instruction: str = Field(min_length=1, max_length=4000)
    reference_id: Identifier | None = None

    @model_validator(mode="after")
    def check_reference(self):
        if self.action == "add" and not self.reference_id:
            raise ValueError("新增版式必须指定参考版式 reference_id")
        if self.action != "add" and self.reference_id is not None:
            raise ValueError("只有新增版式可以指定 reference_id")
        return self


class EditPlan(ContractModel):
    reply: str = Field(default="", max_length=4000)
    operations: tuple[ComponentEdit, ...] = Field(min_length=1, max_length=80)

    def check_targets(self, package, references):
        existing = {c.id for c in package.components}
        targets = [op.component_id for op in self.operations]
        if len(targets) != len(set(targets)):
            raise ValueError("同一版式只能有一个编辑操作")
        count = len(existing)
        for op in self.operations:
            if op.action == "add":
                if op.component_id in existing:
                    raise ValueError(f"新增版式 ID 已存在：{op.component_id}")
                if op.reference_id not in references:
                    raise ValueError(f"参考版式不存在：{op.reference_id}")
                count += 1
            elif op.component_id not in existing:
                raise ValueError(f"待编辑版式不存在：{op.component_id}")
            elif op.action == "delete":
                count -= 1
        if not 1 <= count <= 40:
            raise ValueError("编辑后的模板包必须保留 1–40 个版式")


def summarize(component):
    return component.model_dump(
        mode="json",
        include={"id", "family", "description", "blocks", "metrics", "images"},
    )


def preview_html(package, component):
    """Sample rendering streamed to the editor before anything is saved."""
    from ..slide.package_generation.renderer import render_page

    example = component.examples[0]
    samples = example_assets(package, component)
    return render_page(
        package.model_copy(update={"components": (component,)}),
        component.id,
        example,
        assets=samples,
        allowed_image_urls=frozenset(samples.values()),
    ).html_content


def unique_id(base, taken):
    base = base[:90] or "layout"
    candidate, index = base, 2
    while candidate in taken:
        candidate, index = f"{base}-{index}", index + 1
    return candidate


async def edit_component(
    content, package, op, prompt, reference, history=(), *, emit=None
):
    """One component, validated alone; retried once with the validation error."""
    problem = ""
    for _ in range(2):
        try:
            data, _usage = await reply_completion(
                content,
                '编辑参考页面组件，返回 {"reply":"面向用户的简短调整说明","changes":{需要替换的组件字段}}。'
                "reply 必须放在第一个字段，简洁说明将怎样修改此页，不展示推理过程、JSON、SVG 或内部校验细节，"
                "不能在保存前宣称已经保存。"
                "未提供的字段沿用参考值；slots/examples 等数组若修改必须返回完整数组。"
                "不得修改目标 ID。新增版式必须按要求调整用途描述和构图。"
                "允许同步修改 SVG、槽位、内容容量和示例，所有字段必须符合 schema。"
                "SVG viewBox=0 0 1280 720；文字槽位有 x/y/font-size/data-box-w/data-box-h，"
                "text-anchor=start，字体至少18px，槽位不得预填文字或含子节点。"
                "不得使用脚本、样式表、transform、mask、clip-path；image 无 href。"
                "带 data-asset 的固定底图必须原样保留，不能改色、移除或移动；"
                "data-align(left/center/right)、data-valign(top/middle/bottom) 控制槽位对齐，"
                "图片槽 data-crop 可用 rect/circle/rounded。"
                "保留用户未要求修改的内容，文字和图片框不得越界或重叠，示例必须能完整渲染。\n"
                + json.dumps(
                    {
                        "user_request": prompt,
                        "earlier_rounds": list(history),
                        "operation": op.model_dump(),
                        "theme": package.theme.model_dump(),
                        "reference": reference.model_dump(mode="json"),
                        "schema": PageComponent.model_json_schema(),
                        "previous_error": problem,
                    },
                    ensure_ascii=False,
                ),
                role="template_generation",
                segment_id=f"component:{op.component_id}",
                emit=emit,
            )
            patch = data["changes"]
            if not isinstance(patch, dict) or not patch:
                raise ValueError("changes 必须包含需要修改的字段")
            if patch.get("id", op.component_id) != op.component_id:
                raise ValueError("不可修改目标版式 ID")
            component = PageComponent.model_validate(
                {**reference.model_dump(), **patch, "id": op.component_id}
            )
            from ..slide.svg_page.sanitize import parse_svg, serialize

            def fixed_images(svg):
                return [
                    serialize(n) for n in parse_svg(svg).iter() if n.get("data-asset")
                ]

            if fixed_images(component.svg) != fixed_images(reference.svg):
                raise ValueError("固定底图必须原样保留；可编辑槽位不受此限制")
            candidate = package.model_copy(update={"components": (component,)})
            await asyncio.to_thread(validate_package, candidate)
            return component
        except (ValueError, KeyError, TypeError) as exc:
            problem = str(exc)
    raise ValueError(f"版式 {op.component_id} 未通过校验，未保存任何修改：{problem}")


async def _save(catalog, source, candidate):
    if source.get("editable"):
        # Later rounds refine the same draft in place, so previews stay in one place.
        return await catalog.update_draft(
            source["id"], candidate, source.get("content_hash")
        )
    # Shared system packages are copied; owned packages receive a new version.
    return await catalog.create(
        candidate, source["template_id"] if source["user_id"] else None
    )


def _references(package):
    references = {c.id: c for c in load_builtin_package().components}
    references.update({c.id: c for c in package.components})
    return references


async def edit_package(service, catalog, source, prompt, history=()):
    async with aclosing(
        stream_replies(
            lambda emit: _edit_package(service, catalog, source, prompt, history, emit)
        )
    ) as events:
        async for item in events:
            yield item


async def _edit_package(service, catalog, source, prompt, history, emit):
    """One round, all-or-nothing: do not persist partial edits of this round."""
    package = TemplatePackage.model_validate(source["manifest"])
    content = ContentService(service)
    originals = {c.id: c for c in package.components}
    references = _references(package)
    yield {"type": "progress", "message": "正在规划版式修改…"}
    context = {
        "user_request": prompt,
        "earlier_rounds": list(history),
        "package_name": package.name,
        "components": [summarize(c) for c in package.components],
        "references_for_additions": [summarize(c) for c in references.values()],
        "schema": EditPlan.model_json_schema(),
    }
    problem = ""
    for attempt in range(2):
        try:
            data, _ = await reply_completion(
                content,
                "根据用户要求编辑已有模板包，返回符合 schema 的 JSON，包含 reply 和 operations。"
                "reply 必须放在第一个字段，用用户能理解的语言简洁说明本轮调整，不展示推理过程、代码或内部校验细节，"
                "不能在保存前宣称已经保存。"
                "只规划用户要求的修改(modify)、删除(delete)、新增(add)，不要重建整包。"
                "component_id 使用现有 ID；新增使用不重复的新 ID，并从参考列表选择 reference_id。"
                "instruction 写清每项要求。一个 ID 只能出现一次；删除后至少保留一个版式。"
                "全局配色/风格修改应为所有受影响版式分别列出 modify。"
                "earlier_rounds 是此前几轮已完成的要求，当前 components 已包含其结果。\n"
                + json.dumps(
                    {**context, "previous_error": problem}, ensure_ascii=False
                ),
                role="template_generation",
                segment_id="plan",
                emit=emit,
            )
            plan = EditPlan.model_validate(data)
            plan.check_targets(package, references)
            break
        except ValueError as exc:
            problem = str(exc)
    else:
        raise ValueError(f"编辑计划无效，原模板包未改动：{problem}")

    components = dict(originals)
    changes = []
    labels = {"modify": "修改", "delete": "删除", "add": "新增"}
    for index, op in enumerate(plan.operations):
        yield {
            "type": "progress",
            "current": index,
            "total": len(plan.operations),
            "component_id": op.component_id,
            "message": f"正在{labels[op.action]}版式：{op.component_id}",
        }
        if op.action == "delete":
            del components[op.component_id]
            yield {
                "type": "component",
                "action": "delete",
                "component_id": op.component_id,
            }
        else:
            reference = (
                references[op.reference_id]
                if op.action == "add"
                else originals[op.component_id]
            )
            component = await edit_component(
                content, package, op, prompt, reference, history, emit=emit
            )
            components[op.component_id] = component
            yield {
                "type": "component",
                "action": op.action,
                "component_id": op.component_id,
                "family": component.family,
                "description": component.description,
                "html": await asyncio.to_thread(preview_html, package, component),
            }
        changes.append(
            {
                "action": op.action,
                "component_id": op.component_id,
                "description": op.instruction,
            }
        )

    if tuple(components.values()) == package.components:
        raise ValueError("模型未产生实际修改，请补充具体要求后重试")
    candidate = TemplatePackage.model_validate(
        {**package.model_dump(), "components": tuple(components.values())}
    )
    yield {"type": "progress", "message": "正在校验整包并保存草稿…"}
    await asyncio.to_thread(validate_package, candidate)
    saved = await _save(catalog, source, candidate)
    yield {
        "type": "complete",
        "package": saved,
        "source_version_id": source["id"],
        "changes": changes,
        "message": "修改已保存到草稿，可继续编辑或发布。已发布版本保持不变。",
    }


async def edit_single_component(
    service, catalog, source, action, instruction, component_id=None, reference_id=None
):
    async with aclosing(
        stream_replies(
            lambda emit: _edit_single_component(
                service,
                catalog,
                source,
                action,
                instruction,
                component_id,
                reference_id,
                emit,
            )
        )
    ) as events:
        async for item in events:
            yield item


async def _edit_single_component(
    service, catalog, source, action, instruction, component_id, reference_id, emit
):
    """Modify or add exactly one page, skipping the planner."""
    package = TemplatePackage.model_validate(source["manifest"])
    references = _references(package)
    existing = {c.id for c in package.components}
    if action == "modify":
        if component_id not in existing:
            raise ValueError("待编辑版式不存在")
        reference = references[component_id]
    else:
        if reference_id not in references:
            raise ValueError("参考版式不存在")
        if len(existing) >= 40:
            raise ValueError("模板包最多 40 个版式")
        reference = references[reference_id]
        component_id = unique_id(f"{reference.family}-custom", existing)
    op = ComponentEdit(
        action=action,
        component_id=component_id,
        instruction=instruction,
        reference_id=reference_id if action == "add" else None,
    )
    yield {
        "type": "progress",
        "component_id": component_id,
        "message": "正在修改版式…" if action == "modify" else "正在设计新版式…",
    }
    component = await edit_component(
        ContentService(service), package, op, instruction, reference, emit=emit
    )
    yield {
        "type": "component",
        "action": action,
        "component_id": component_id,
        "family": component.family,
        "description": component.description,
        "html": await asyncio.to_thread(preview_html, package, component),
    }
    components = [component if c.id == component_id else c for c in package.components]
    if action == "add":
        components.append(component)
    candidate = TemplatePackage.model_validate(
        {**package.model_dump(), "components": tuple(components)}
    )
    yield {"type": "progress", "message": "正在校验整包并保存草稿…"}
    saved = await _save(catalog, source, candidate)
    yield {
        "type": "complete",
        "package": saved,
        "source_version_id": source["id"],
        "changes": [
            {"action": action, "component_id": component_id, "description": instruction}
        ],
        "message": "已保存到草稿。",
    }
