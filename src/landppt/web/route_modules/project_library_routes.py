"""
项目素材与模板相关页面路由。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...auth.middleware import get_current_user_required
from ...database.models import User
from .support import logger, ppt_service, templates

router = APIRouter()


@router.get("/global-master-templates", response_class=HTMLResponse)
async def global_master_templates_page(
    request: Request,
    user: User = Depends(get_current_user_required),
):
    """全局母版模板管理页。"""
    try:
        return templates.TemplateResponse(
            "pages/template/global_master_templates.html",
            {"request": request, "user": user},
        )
    except Exception as exc:
        logger.error("Error loading global master templates page: %s", exc)
        return templates.TemplateResponse("error.html", {"request": request, "error": str(exc)})


@router.get("/image-gallery", response_class=HTMLResponse)
async def image_gallery_page(
    request: Request,
    user: User = Depends(get_current_user_required),
):
    """本地图床管理页。"""
    try:
        return templates.TemplateResponse(
            "pages/image/image_gallery.html",
            {"request": request, "user": user},
        )
    except Exception as exc:
        logger.error("Error rendering image gallery page: %s", exc)
        return templates.TemplateResponse("error.html", {"request": request, "error": str(exc)})


@router.get("/image-generation-test", response_class=HTMLResponse)
async def image_generation_test_page(
    request: Request,
    user: User = Depends(get_current_user_required),
):
    """图片生成测试页。"""
    try:
        return templates.TemplateResponse(
            "pages/image/image_generation_test.html",
            {"request": request, "user": user},
        )
    except Exception as exc:
        logger.error("Error rendering image generation test page: %s", exc)
        return templates.TemplateResponse("error.html", {"request": request, "error": str(exc)})


async def _apply_preferred_package(project, preference, user_id) -> str:
    """Bind the preferred version; returns a user-facing reason on failure."""
    from ...services.slide.package_generation.options import PackageOptions
    from ...services.slide.package_generation.storage import PackageStorage

    metadata = project.project_metadata or {}
    version_id = preference.get("version_id")
    if (
        metadata.get("generation_mode") == "package"
        and metadata.get("selected_package_version_id") == version_id
    ):
        return ""  # Already bound; re-selecting would reset page layouts.
    try:
        await PackageStorage(user_id).select_package(
            project.project_id, version_id, PackageOptions()
        )
    except ValueError as exc:
        logger.warning("Preferred package %s not applied: %s", version_id, exc)
        return f"需求确认时选择的模板包无法使用（{exc}），请重新选择。"
    return ""


@router.get("/projects/{project_id}/template-selection", response_class=HTMLResponse)
async def template_selection_page(
    request: Request,
    project_id: str,
    user: User = Depends(get_current_user_required),
):
    """模板选择页。"""
    try:
        project = await ppt_service.project_manager.get_project(project_id, user_id=user.id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        preference = (project.confirmed_requirements or {}).get("generation_preference") or {}
        package_notice = ""
        # The package was already chosen with the requirements (and shaped the
        # outline); skip picking it again. An explicit ?mode= still opens the page.
        if preference.get("mode") == "package" and "mode" not in request.query_params:
            package_notice = await _apply_preferred_package(project, preference, user.id)
            if not package_notice:
                return RedirectResponse(
                    f"/projects/{project_id}/todo-editor?auto_start=true", status_code=303
                )

        return templates.TemplateResponse(
            "pages/template/template_selection.html",
            {
                "request": request,
                "project_id": project_id,
                "project_topic": project.topic,
                # Chosen during requirement confirmation; the outline already fits it.
                "package_preference": (project.confirmed_requirements or {}).get(
                    "generation_preference"
                )
                or {},
                "package_notice": package_notice,
            },
        )
    except Exception as exc:
        logger.error("Error loading template selection page: %s", exc)
        return templates.TemplateResponse("error.html", {"request": request, "error": str(exc)})
