"""Authenticated management, selection and structured editing for package pages."""

import asyncio
import json
import time
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..auth.middleware import get_current_user_required
from ..services.slide.package_generation.candidate_filter import compatible_components
from ..services.slide.package_generation.content_service import ContentService
from ..services.slide.package_generation.jev_client import (
    DEFAULT_DECISION_ENDPOINT,
    MAX_JEV_CONCURRENCY,
    JevClient,
)
from ..services.slide.package_generation.options import PackageOptions
from ..services.slide.package_generation.renderer import render_page
from ..services.slide.package_generation.storage import PackageStorage
from ..services.template_package.catalog import (
    PackageCatalog,
    PackageConflict,
    PackageNotFound,
)
from ..services.template_package.schemas import PageContent, TemplatePackage
from ..services.template_package.validator import VALIDATION_IMAGE

router = APIRouter(tags=["Template packages"])


def catalog(user=Depends(get_current_user_required)):
    return PackageCatalog(user.id)


def storage(user=Depends(get_current_user_required)):
    return PackageStorage(user.id)


async def checked(awaitable):
    try:
        return await awaitable
    except PackageNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except PackageConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImportPackage(RequestModel):
    manifest: TemplatePackage
    template_id: int | None = None


class SelectPackage(RequestModel):
    version_id: int
    options: PackageOptions = Field(default_factory=PackageOptions)


class ContentUpdate(RequestModel):
    expected_revision: int = Field(ge=0)
    content: PageContent


class PageAction(RequestModel):
    expected_revision: int = Field(ge=0)
    component_id: str | None = None
    action: Literal["layout", "manual", "restore", "lock", "unlock", "image"] = "layout"
    visual_id: str | None = None
    image_id: str | None = None


class EditInstruction(RequestModel):
    expected_revision: int = Field(ge=0)
    instruction: str = Field(min_length=1, max_length=4000)


class GeneratePackage(RequestModel):
    prompt: str = Field(min_length=1, max_length=6000)


class SelectorSettings(RequestModel):
    protocol: Literal["jev", "openai"] = "jev"
    endpoint_url: str = Field(DEFAULT_DECISION_ENDPOINT, max_length=2000)
    enabled: bool = False
    api_key: str | None = Field(None, max_length=500)
    model: str = Field("jev-latest", min_length=1, max_length=100)
    timeout: int = Field(30, ge=1, le=120)
    retries: int = Field(2, ge=0, le=3)
    concurrency: int = Field(1, ge=1, le=MAX_JEV_CONCURRENCY)

    @field_validator("endpoint_url")
    @classmethod
    def validate_endpoint(cls, value):
        value = value.strip()
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in value)
        ):
            raise ValueError("端点必须是有效的 HTTP(S) URL，不能包含用户名、密码或片段")
        _ = parsed.port
        return value

    @field_validator("model")
    @classmethod
    def validate_model(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("模型名称不能为空")
        return value


@router.get("/api/global-master-templates/packages")
async def list_packages(service=Depends(catalog)):
    return {"packages": await checked(service.list())}


@router.post("/api/global-master-templates/packages")
async def create_package(payload: ImportPackage, service=Depends(catalog)):
    return await checked(service.create(payload.manifest, payload.template_id))


@router.post("/api/global-master-templates/packages/builtin")
async def install_builtin(service=Depends(catalog)):
    return await checked(service.install_builtin())


@router.get("/api/global-master-templates/packages/settings")
async def selector_settings(user=Depends(get_current_user_required)):
    from ..services.db_config_service import get_db_config_service

    config = await get_db_config_service().get_all_config(user_id=user.id)
    return {
        "enabled": bool(config.get("jev_enabled")),
        "configured": bool(config.get("jev_api_key")),
        "model": config.get("jev_model") or "jev-latest",
        "endpoint_url": config.get("jev_endpoint_url") or DEFAULT_DECISION_ENDPOINT,
        "protocol": config.get("jev_protocol") or "jev",
        "timeout": config.get("jev_timeout", 30),
        "retries": config.get("jev_retries", 2),
        "concurrency": config.get("jev_concurrency", 1),
    }


@router.put("/api/global-master-templates/packages/settings")
async def update_selector_settings(
    payload: SelectorSettings, user=Depends(get_current_user_required)
):
    from ..services.db_config_service import get_db_config_service

    values = {
        "jev_enabled": payload.enabled,
        "jev_model": payload.model,
        "jev_endpoint_url": payload.endpoint_url,
        "jev_protocol": payload.protocol,
        "jev_timeout": payload.timeout,
        "jev_retries": payload.retries,
        "jev_concurrency": payload.concurrency,
    }
    if payload.api_key is not None:
        values["jev_api_key"] = payload.api_key
    if not await get_db_config_service().update_config(values, user_id=user.id):
        raise HTTPException(500, "设置保存失败")
    return {"success": True}


@router.post("/api/global-master-templates/packages/settings/test")
async def test_selector_settings(
    payload: SelectorSettings, user=Depends(get_current_user_required)
):
    from ..services.db_config_service import get_db_config_service
    from ..services.template_package.builtin import load_builtin_package

    config = await get_db_config_service().get_all_config(user_id=user.id)
    key = (payload.api_key or "").strip() or config.get("jev_api_key")
    if not key:
        raise HTTPException(422, "请先输入 API Key 或保存密钥")
    component = load_builtin_package().components[0]
    page = component.examples[0]
    started = time.monotonic()
    try:
        scores, calls = await JevClient(
            key,
            payload.model,
            payload.timeout,
            retries=0,
            concurrency=1,
            endpoint_url=payload.endpoint_url,
            protocol=payload.protocol,
        ).evaluate([page], {page.slide_id: [component]})
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        detail = (
            "认证失败，请检查 API Key"
            if code in {401, 403}
            else f"接口返回 HTTP {code}，请检查端点和模型名称"
        )
        raise HTTPException(502, detail) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "测试超时，请检查端点或增大超时时间") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, "无法连接决策模型端点，请检查地址和网络") from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            502, "接口响应不符合决策评分协议，请检查接口协议和模型名称"
        ) from exc
    if not scores:
        raise HTTPException(502, "接口未返回有效的决策评分")
    return {
        "success": True,
        "model": calls[0].get("model") or payload.model,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }


@router.get("/api/global-master-templates/packages/{version_id}")
async def get_package(version_id: int, service=Depends(catalog)):
    return await checked(service.get(version_id))


@router.post("/api/global-master-templates/packages/{version_id}/{action}")
async def transition_package(
    version_id: int,
    action: Literal["validate", "publish", "retire"],
    service=Depends(catalog),
):
    return await checked(service.transition(version_id, action))


@router.get("/api/global-master-templates/packages/{version_id}/preview/{component_id}")
async def preview_package(version_id: int, component_id: str, service=Depends(catalog)):
    version = await checked(service.get(version_id))
    package = TemplatePackage.model_validate(version["manifest"])
    component = next((c for c in package.components if c.id == component_id), None)
    if not component:
        raise HTTPException(404, "组件不存在")
    content = component.examples[0]
    try:
        result = await asyncio.to_thread(
            render_page,
            package,
            component_id,
            content,
            assets={v.id: VALIDATION_IMAGE for v in content.visual_briefs},
            allowed_image_urls=frozenset({VALIDATION_IMAGE}),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return HTMLResponse(
        result.html_content,
        headers={
            "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:;"
        },
    )


@router.post("/api/projects/{project_id}/template-package")
async def select_package(
    project_id: str, payload: SelectPackage, repo=Depends(storage)
):
    return await checked(
        repo.select_package(project_id, payload.version_id, payload.options)
    )


@router.get("/api/projects/{project_id}/template-package")
async def project_package(project_id: str, repo=Depends(storage)):
    return await checked(repo.snapshot(project_id))


async def page_context(repo, project_id, slide_id, revision=None):
    snapshot = await checked(repo.snapshot(project_id))
    page = next((p for p in snapshot["pages"] if p["slide_id"] == slide_id), None)
    if not page:
        raise HTTPException(404, "页面不存在")
    if revision is not None and revision != page["revision"]:
        raise HTTPException(409, "页面已被修改，请刷新后重试")
    return snapshot, page


@router.get("/api/projects/{project_id}/package-pages/{slide_id}/layouts")
async def page_layouts(project_id: str, slide_id: str, repo=Depends(storage)):
    snapshot, page = await page_context(repo, project_id, slide_id)
    if not page["content"]:
        return {"components": []}
    options = PackageOptions.model_validate(snapshot["options"])
    package = TemplatePackage.model_validate(snapshot["manifest"])
    content = PageContent.model_validate(page["content"])
    return {
        "components": [
            {"id": c.id, "family": c.family, "description": c.description}
            for c in compatible_components(
                package,
                content,
                allow_images=options.allow_images,
                image_budget=options.image_budget,
            )
        ]
    }


async def render_existing(
    repo,
    project_id,
    snapshot,
    page,
    *,
    component_id=None,
    restore=False,
    content_update=None,
    assets=None,
    usage=None,
    bill=False,
):
    if page["manual"] and not restore:
        raise HTTPException(409, "此页已自由编辑，请先恢复模板包控制")
    package = TemplatePackage.model_validate(snapshot["manifest"])
    if not page["content"] and content_update is None:
        raise HTTPException(409, "请先生成页面内容")
    content = content_update or PageContent.model_validate(page["content"])
    options = PackageOptions.model_validate(snapshot["options"])
    remaining = options.image_budget - sum(
        len(p["assets"] or {})
        for p in snapshot["pages"]
        if p["slide_id"] != page["slide_id"]
    )
    choices = compatible_components(
        package,
        content,
        allow_images=options.allow_images,
        image_budget=max(0, remaining),
    )
    selected = (
        component_id or page["component_id"] or (choices[0].id if choices else None)
    )
    if component_id is None and selected not in {c.id for c in choices} and choices:
        selected = choices[0].id
    if selected not in {c.id for c in choices}:
        raise HTTPException(422, "内容不适合当前版式，请调整内容或使用自由设计")
    resolved = assets if assets is not None else (page["assets"] or {})
    resolved = {
        k: v for k, v in resolved.items() if k in {b.id for b in content.visual_briefs}
    }
    try:
        result = await asyncio.to_thread(
            render_page,
            package,
            selected,
            content,
            assets=resolved,
            allowed_image_urls=frozenset(resolved.values()),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await checked(
        repo.commit_render(
            project_id,
            page["slide_id"],
            page["revision"],
            result,
            restore=restore,
            content_update=content_update,
            assets=resolved,
            usage=usage,
            bill=bill,
            operation="ai_edit" if bill else "slide_generation",
        )
    )
    return await checked(repo.snapshot(project_id))


@router.put("/api/projects/{project_id}/package-pages/{slide_id}/content")
async def update_content(
    project_id: str, slide_id: str, payload: ContentUpdate, repo=Depends(storage)
):
    snapshot, page = await page_context(
        repo, project_id, slide_id, payload.expected_revision
    )
    if payload.content.slide_id != slide_id:
        raise HTTPException(422, "内容的页面标识不一致")
    if page["manual"] or page["locked"]:
        raise HTTPException(409, "请先恢复模板包控制并解锁页面")
    options = PackageOptions.model_validate(snapshot["options"])
    other_images = sum(
        len(p["assets"]) for p in snapshot["pages"] if p["slide_id"] != slide_id
    )
    if (payload.content.visual_briefs and not options.allow_images) or len(
        payload.content.visual_briefs
    ) + other_images > options.image_budget:
        raise HTTPException(422, "配图数量超过项目设置")
    content = payload.content.model_copy(
        update={"content_revision": page["content_revision"] + 1}
    )
    return await render_existing(
        repo,
        project_id,
        snapshot,
        page,
        content_update=content,
        usage={"edited_by_user": True},
    )


@router.post("/api/projects/{project_id}/package-pages/{slide_id}/action")
async def page_action(
    project_id: str, slide_id: str, payload: PageAction, repo=Depends(storage)
):
    snapshot, page = await page_context(
        repo, project_id, slide_id, payload.expected_revision
    )
    if payload.action in {"manual", "lock", "unlock"}:
        await checked(
            repo.mark(
                project_id,
                slide_id,
                page["revision"],
                manual=True if payload.action == "manual" else None,
                locked=payload.action == "lock" if payload.action != "manual" else None,
            )
        )
        return await checked(repo.snapshot(project_id))
    if page["locked"]:
        raise HTTPException(409, "请先解锁页面")
    if payload.action == "image":
        if not payload.visual_id or payload.visual_id not in {
            v["id"] for v in (page["content"] or {}).get("visual_briefs", [])
        }:
            raise HTTPException(422, "图片槽位不存在")
        # Resolve a gallery ID under this user's image service, never accept arbitrary URLs.
        from ..services.url_service import build_image_url
        from ..web.route_modules.support import get_ppt_service_for_user

        service = get_ppt_service_for_user(repo.user_id)
        if not service.image_service:
            raise HTTPException(409, "图片服务未启用")
        image = await service.image_service.get_image(payload.image_id or "")
        if not image or image.owner_user_id != repo.user_id:
            raise HTTPException(404, "图片不存在或无权访问")
        url = build_image_url(
            image.image_id, width=image.metadata.width, height=image.metadata.height
        )
        return await render_existing(
            repo,
            project_id,
            snapshot,
            page,
            component_id=payload.component_id,
            assets={**page["assets"], payload.visual_id: url},
        )
    return await render_existing(
        repo,
        project_id,
        snapshot,
        page,
        component_id=payload.component_id,
        restore=payload.action == "restore",
    )


@router.post("/api/projects/{project_id}/package-pages/{slide_id}/edit")
async def ai_edit_content(
    project_id: str, slide_id: str, payload: EditInstruction, repo=Depends(storage)
):
    from ..web.route_modules.support import get_ppt_service_for_user

    snapshot, page = await page_context(
        repo, project_id, slide_id, payload.expected_revision
    )
    if page["manual"] or page["locked"]:
        raise HTTPException(409, "请先恢复模板包控制并解锁页面")
    if not page["content"]:
        raise HTTPException(409, "请先生成页面内容")
    service = get_ppt_service_for_user(repo.user_id)
    from ..core.config import app_config

    _, settings = await service.get_role_provider_async("slide_generation")
    content, usage = await checked(
        ContentService(service).repair(
            PageContent.model_validate(page["content"]),
            TemplatePackage.model_validate(snapshot["manifest"]),
            [],
            instruction=payload.instruction,
        )
    )
    content = content.model_copy(
        update={"content_revision": page["content_revision"] + 1}
    )
    return await render_existing(
        repo,
        project_id,
        snapshot,
        page,
        content_update=content,
        usage=usage,
        bill=bool(
            app_config.enable_credits_system and settings.get("provider") == "landppt"
        ),
    )


@router.post("/api/global-master-templates/packages-generate")
async def create_package_with_ai(
    payload: GeneratePackage, user=Depends(get_current_user_required)
):
    from ..services.template_package.generator import generate_package
    from ..web.route_modules.support import (
        check_credits_for_operation,
        consume_credits_for_operation,
        get_ppt_service_for_user,
    )

    service = get_ppt_service_for_user(user.id)
    _, settings = await service.get_role_provider_async("template_generation")
    ok, required, balance = await check_credits_for_operation(
        user.id, "template_generation", 1, provider_name=settings.get("provider")
    )
    if not ok:
        raise HTTPException(402, "积分不足")

    async def stream():
        try:
            async for item in generate_package(service, payload.prompt):
                if item["type"] == "complete":
                    await consume_credits_for_operation(
                        user.id,
                        "template_generation",
                        1,
                        description="AI 模板包创建",
                        reference_id=f"package-version:{item['package']['id']}",
                        provider_name=settings.get("provider"),
                    )
                yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
        except Exception as exc:
            yield "data: " + json.dumps(
                {"type": "error", "message": str(exc)}, ensure_ascii=False
            ) + "\n\n"

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )
