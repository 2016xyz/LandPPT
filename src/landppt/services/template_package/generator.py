"""Generate reusable designs component by component, leaving a reviewable draft."""

import asyncio
import uuid

from ..slide.package_generation.content_service import ContentService
from .builtin import load_builtin_package
from .catalog import PackageCatalog
from .schemas import PackageTheme, PageComponent, TemplatePackage
from .service import validate_package


async def generate_package(service, prompt):
    content = ContentService(service)
    base = load_builtin_package()
    plan, usage = await content.json_completion(
        "为可复用 SVG 演示模板包设计主题，返回 JSON：name、description、theme。"
        "theme 仅包含 background、foreground、accent、muted 四个 #RRGGBB 色值。"
        "保持正文高对比度和清晰层级。用户要求：" + prompt,
        role="template_generation",
    )
    theme = PackageTheme.model_validate(plan["theme"])
    package_id = "ai_" + uuid.uuid4().hex
    components, failures, calls = [], [], [usage]
    for index, original in enumerate(base.components):
        yield {
            "type": "progress",
            "current": index,
            "total": len(base.components),
            "message": f"正在设计 {original.family} 版式",
        }
        error = ""
        for attempt in range(2):
            import json

            try:
                data, usage = await content.json_completion(
                    '为以下完整页面组件重新设计 SVG。返回 {"svg":"..."}。保留全部绑定节点 ID 与节点类型，'
                    "text 节点必须有 x/y/font-size/data-box-w/data-box-h，text-anchor=start。"
                    "槽位内不可预填内容或含子节点；image 不可携带 href；不得使用脚本、样式表、transform、mask、clip-path。"
                    "画布 viewBox=0 0 1280 720，字体至少 18px。以清晰、有变化的构图表达内容，避免重复卡片。"
                    "文本框和图片不得超出画布；主题色需要直接写入 SVG 属性。\n"
                    + json.dumps(
                        {
                            "user_request": prompt,
                            "theme": theme.model_dump(),
                            "component": original.model_dump(mode="json"),
                            "previous_error": error,
                        },
                        ensure_ascii=False,
                    ),
                    role="template_generation",
                )
                calls.append(usage)
                component = PageComponent.model_validate(
                    {**original.model_dump(), "svg": data["svg"]}
                )
                candidate = TemplatePackage(
                    package_id=package_id,
                    version=1,
                    name=plan["name"],
                    description=plan.get("description", ""),
                    theme=theme,
                    components=(component,),
                )
                await asyncio.to_thread(validate_package, candidate)
                components.append(component)
                break
            except (ValueError, KeyError, TypeError) as exc:
                error = str(exc)
        else:
            failures.append({"component_id": original.id, "error": error})
    if not components:
        raise ValueError("没有组件通过校验，模板包未保存")
    manifest = TemplatePackage(
        package_id=package_id,
        version=1,
        name=plan["name"],
        description=plan.get("description", ""),
        theme=theme,
        components=tuple(components),
    )
    saved = await PackageCatalog(service.user_id).create(manifest)
    # Even a partial package remains a draft; the user reviews before publishing.
    yield {
        "type": "complete",
        "partial": bool(failures),
        "failures": failures,
        "package": saved,
        "usage": calls,
        "message": "已保存草稿，请预览并发布",
    }
