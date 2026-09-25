"""Exercise package UI, real API/database edits, and existing SVG exporters offline.

uv run python scripts/template_package_smoke.py
No user database, model API, or paid image service is used.
"""

import argparse
import asyncio
import base64
import json
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import fitz
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from playwright.async_api import async_playwright
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from landppt.api import template_package_api as api
from landppt.database.models import Base, Project, User
from landppt.services.slide.package_generation.jev_client import JevClient
from landppt.services.slide.package_generation.options import PackageOptions
from landppt.services.slide.package_generation.renderer import render_page
from landppt.services.slide.package_generation.storage import PackageStorage
from landppt.services.template_package.builtin import load_builtin_package
from landppt.services.template_package.catalog import PackageCatalog
from landppt.services.template_package.schemas import PageContent
from landppt.services.template_package.validator import VALIDATION_IMAGE


async def main(ui_only=False):
    out = Path("artifacts/template-package-integration")
    out.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{out / ('smoke-' + uuid.uuid4().hex + '.db')}"
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with sessions() as session, session.begin():
        session.add(User(id=1, username="smoke", password_hash="unused"))
        session.add(
            Project(
                project_id="smoke",
                user_id=1,
                title="模板包流程验证",
                topic="测试",
                scenario="general",
                outline={"slides": [{"title": "封面"}]},
            )
        )
    catalog, storage = PackageCatalog(1, sessions), PackageStorage(1, sessions)
    saved = await catalog.install_builtin()
    snapshot = await storage.select_package(
        "smoke", saved["id"], PackageOptions(selector="rules")
    )
    sid = snapshot["pages"][0]["slide_id"]
    await api.update_content(
        "smoke",
        sid,
        api.ContentUpdate(
            expected_revision=0,
            content=PageContent(slide_id=sid, title="把内容变成清晰的表达"),
        ),
        storage,
    )
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.catalog] = lambda: catalog
    app.dependency_overrides[api.storage] = lambda: storage
    app.dependency_overrides[api.get_current_user_required] = lambda: SimpleNamespace(
        id=1
    )
    app.mount("/static", StaticFiles(directory="src/landppt/web/static"))
    env = Environment(
        loader=FileSystemLoader("src/landppt/web/templates"), autoescape=True
    )
    user = {"id": 1, "username": "smoke", "is_admin": False}

    @app.get("/global-master-templates", response_class=HTMLResponse)
    async def template_management_page():
        return env.get_template("pages/template/global_master_templates.html").render(
            user=user
        )

    @app.get("/projects/smoke/template-selection", response_class=HTMLResponse)
    async def select_page():
        return env.get_template("pages/template/template_selection.html").render(
            user=user, project_id="smoke", project_topic="模板包流程验证"
        )

    @app.get("/ai-config", response_class=HTMLResponse)
    async def settings_page():
        return env.get_template("pages/settings/ai_config.html").render(
            user=user,
            current_config={},
            current_provider="openai",
            provider_status={},
            available_providers=[],
        )

    @app.get("/api/global-master-templates/")
    async def regular_templates():
        return {
            "templates": [],
            "pagination": {"current_page": 1, "total_pages": 1, "total_count": 0},
        }

    @app.get("/projects/smoke/todo-editor", response_class=HTMLResponse)
    async def selected_generator():
        snapshot = await storage.snapshot("smoke")
        return env.get_template("pages/project/todo_board_with_editor.html").render(
            project={
                "project_id": "smoke",
                "topic": "入口验证",
                "project_metadata": snapshot["metadata"],
                "outline": snapshot["outline"],
                "slides_data": [],
                "status": "draft",
            },
            todo_board={"task_id": "smoke", "title": "快速生成入口验证", "stages": []},
            unattended_active=False,
        )

    @app.get("/api/projects/smoke")
    async def project_metadata():
        snapshot = await storage.snapshot("smoke")
        return {
            "project_metadata": snapshot["metadata"],
            "outline": snapshot["outline"],
            "slides_data": [],
        }

    stream_requests = []

    @app.get("/api/projects/smoke/slides/stream")
    async def generation_stream():
        stream_requests.append(True)
        return StreamingResponse(
            iter(['data: {"type":"complete","total":1,"message":"入口验证完成"}\n\n']),
            media_type="text/event-stream",
        )

    @app.get("/editor", response_class=HTMLResponse)
    async def editor_page():
        # Use the actual editor toolbar and its CSS; unrelated editor integrations
        # (CodeMirror, media and export) remain outside this offline UI fixture.
        snapshot = await storage.snapshot("smoke")
        rendered = env.get_template("pages/project/project_slides_editor.html").render(
            project={
                "project_id": "smoke",
                "title": "模板包编辑验证",
                "topic": "测试",
                "scenario": "general",
                "requirements": "",
                "status": "completed",
                "outline": snapshot["outline"],
                "project_metadata": snapshot["metadata"],
                "slides_data": [],
            },
            narration_video_tools_enabled=False,
        )
        toolbar = BeautifulSoup(rendered, "html.parser").select_one(".editor-toolbar")
        return (
            '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            '<link rel="stylesheet" href="/static/css/pages/project/slides_editor/projectSlidesEditor.css">'
            '<link rel="stylesheet" href="/static/css/template-packages.css">'
            "<style>*{box-sizing:border-box}.dropdown-menu{display:none}.toolbar-btn>i{display:inline-block;width:1em}"
            "@media(max-width:576px){.smoke-sidebar{display:none}}</style>"
            '<main class="editor-container"><div class="d-flex" style="display:flex">'
            '<aside class="slides-sidebar smoke-sidebar" style="flex:0 0 260px">幻灯片</aside>'
            f'<div class="main-editor" style="flex:1">{toolbar}</div></div></main>'
            '<script>window.projectId="smoke";let currentSlideIndex=0;</script>'
            '<script src="/static/js/template-packages.js"></script>'
        )

    report = {"uses_live_models": False}
    # The settings endpoint must also stay inside this isolated fixture.
    settings = {}

    async def save_settings(values, **kwargs):
        settings.update(values)
        return True

    config_patch = patch(
        "landppt.services.db_config_service.get_db_config_service",
        return_value=SimpleNamespace(
            get_all_config=AsyncMock(side_effect=lambda **kwargs: dict(settings)),
            update_config=AsyncMock(side_effect=save_settings),
        ),
    )
    config_patch.start()
    decision_requests = []

    def decision_response(request):
        decision_requests.append(request)
        if request.headers.get("authorization") != "Bearer smoke-only-placeholder":
            return httpx.Response(401)
        data = json.loads(request.content)
        assert str(request.url) == "https://decision.example/v1/chat/completions"
        assert data["model"] == "custom-decision-model" and "messages" in data
        return httpx.Response(
            200,
            json={
                "model": data["model"],
                "choices": [
                    {
                        "message": {"content": '{"scores":{"q0":0.9}}'},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    decision_patch = patch.object(
        api,
        "JevClient",
        lambda *a, **kw: JevClient(
            *a, **kw, transport=httpx.MockTransport(decision_response)
        ),
    )
    decision_patch.start()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://package.test"
        ) as client:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                context = await browser.new_context(
                    viewport={"width": 1440, "height": 1000}
                )

                async def forward(route):
                    req = route.request
                    response = await client.request(
                        req.method, req.url, content=req.post_data_buffer
                    )
                    await route.fulfill(
                        status=response.status_code,
                        body=response.content,
                        headers={
                            k: v
                            for k, v in response.headers.items()
                            if k not in {"content-length", "content-encoding"}
                        },
                    )

                await context.route("http://package.test/**", forward)
                await context.route("https://**", lambda route: route.abort())
                # Bootstrap's unrelated modal constructor is not under test offline.
                await context.add_init_script("window.bootstrap={Modal:class{}}")
                page = await context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto("http://package.test/global-master-templates")
                regular_tab = page.get_by_role("tab", name="普通模板", exact=True)
                package_tab = page.get_by_role("tab", name="快速模式模板包", exact=True)
                assert await regular_tab.get_attribute("aria-selected") == "true"
                assert not await page.locator("#templatePackagePanel").is_visible()
                await page.locator("#createTemplateBtn").click()
                await page.locator("#templateModal").wait_for(state="visible")
                await page.locator("#cancelBtn").click()
                await page.screenshot(
                    path=str(out / "management-regular.png"), full_page=True
                )
                await package_tab.click()
                assert not await page.locator("#createTemplateBtn").is_visible()
                await page.locator("#packageCreate").click()
                await page.get_by_role("dialog").wait_for()
                await page.get_by_role("button", name="关闭", exact=True).click()
                await page.frame_locator(".package-card iframe").locator(
                    "svg"
                ).wait_for()
                await page.screenshot(
                    path=str(out / "management-packages.png"), full_page=True
                )
                await package_tab.focus()
                await page.keyboard.press("ArrowLeft")
                assert await regular_tab.get_attribute("aria-selected") == "true"
                assert await page.locator("#createTemplateBtn").is_visible()
                await page.keyboard.press("End")
                await page.reload()
                assert await package_tab.get_attribute("aria-selected") == "true"
                assert not await page.locator("#regularTemplatesPanel").is_visible()
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.locator(".package-card-preview").scroll_into_view_if_needed()
                await page.screenshot(
                    path=str(out / "management-packages-mobile.png"), full_page=True
                )
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                await page.set_viewport_size({"width": 1440, "height": 1000})
                await page.goto("http://package.test/ai-config")
                await page.get_by_role(
                    "button", name="决策模型配置", exact=True
                ).click()
                await page.locator("#packageJevModel").wait_for(state="visible")
                await page.locator("#packageJevKey").fill("smoke-only-placeholder")
                await page.locator("#packageJevEnabled").check()
                await page.locator("#packageJevProtocol").select_option("openai")
                await page.locator("#packageJevEndpoint").fill(
                    "https://decision.example/v1/chat/completions"
                )
                await page.locator("#packageJevModel").fill("custom-decision-model")
                await page.locator("#packageJevConcurrency").fill("16")
                await page.get_by_role("button", name="测试连接", exact=True).click()
                await page.get_by_text("测试成功：", exact=False).wait_for()
                assert settings == {}, "Connection test must not save the form"
                await page.get_by_role("button", name="保存配置", exact=True).click()
                await page.get_by_text("决策模型配置已保存。", exact=True).wait_for()
                assert settings["jev_enabled"] is True
                assert settings["jev_model"] == "custom-decision-model"
                assert settings["jev_protocol"] == "openai"
                assert (
                    settings["jev_endpoint_url"]
                    == "https://decision.example/v1/chat/completions"
                )
                assert settings["jev_concurrency"] == 16
                assert await page.locator("#packageJevKey").input_value() == ""
                await page.goto("http://package.test/ai-config#jev-config")
                await page.get_by_text(
                    "已保存 API Key；留空可保留，输入新密钥可替换。", exact=True
                ).wait_for()
                assert (
                    await page.locator("#packageJevConcurrency").input_value() == "16"
                )
                assert (
                    await page.locator("#packageJevEndpoint").input_value()
                    == settings["jev_endpoint_url"]
                )
                assert (
                    await page.locator("#packageJevModel").input_value()
                    == settings["jev_model"]
                )
                await page.get_by_role("button", name="测试连接", exact=True).click()
                await page.get_by_text("测试成功：", exact=False).wait_for()
                await page.locator("#packageJevKey").fill("bad-test-key")
                await page.get_by_role("button", name="测试连接", exact=True).click()
                await page.get_by_text(
                    "认证失败，请检查 API Key", exact=True
                ).wait_for()
                assert settings["jev_api_key"] == "smoke-only-placeholder"
                assert len(decision_requests) == 3
                await page.locator("#packageJevKey").fill("")
                await page.get_by_role("button", name="测试连接", exact=True).click()
                await page.get_by_text("测试成功：", exact=False).wait_for()
                await page.screenshot(
                    path=str(out / "jev-settings.png"), full_page=True
                )
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.screenshot(
                    path=str(out / "jev-settings-mobile.png"), full_page=True
                )
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                ), "Settings page overflows on mobile"
                await page.set_viewport_size({"width": 1440, "height": 1000})
                await page.goto("http://package.test/projects/smoke/template-selection")
                assert await page.locator("#regularTemplatePanel").is_visible()
                assert not await page.locator("#templatePackagePanel").is_visible()
                await page.get_by_role(
                    "button", name="快速模式生成", exact=False
                ).click()
                assert not await page.locator("#regularTemplatePanel").is_visible()
                await page.get_by_role("button", name="预览", exact=True).wait_for()
                await page.frame_locator(".package-card iframe").locator(
                    "svg"
                ).wait_for()
                await page.screenshot(path=str(out / "catalog.png"), full_page=True)
                assert not await page.get_by_role(
                    "button", name="停用此版本", exact=True
                ).is_visible()
                await page.get_by_text("更多", exact=True).click()
                assert await page.get_by_role(
                    "button", name="停用此版本", exact=True
                ).is_visible()
                await page.get_by_text("更多", exact=True).click()
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.locator(".package-card-preview").scroll_into_view_if_needed()
                await page.frame_locator(".package-card iframe").locator(
                    "svg"
                ).wait_for()
                await page.wait_for_function(
                    "Math.abs(document.querySelector('.package-card iframe').getBoundingClientRect().width - document.querySelector('.package-card .package-preview-holder').clientWidth) < 2"
                )
                await page.screenshot(
                    path=str(out / "catalog-mobile.png"), full_page=True
                )
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                ), "Template picker overflows on mobile"
                await page.set_viewport_size({"width": 1440, "height": 1000})
                await page.get_by_role("button", name="预览", exact=True).click()
                await page.get_by_label("预览版式").select_option(index=4)
                await page.screenshot(path=str(out / "preview.png"), full_page=True)
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.screenshot(
                    path=str(out / "preview-mobile.png"), full_page=True
                )
                await page.wait_for_function(
                    "Math.abs(document.querySelector('.package-dialog iframe').getBoundingClientRect().width - document.querySelector('.package-dialog .package-preview-holder').clientWidth) < 2"
                )
                await page.get_by_role("button", name="关闭", exact=True).click()
                await page.set_viewport_size({"width": 1440, "height": 1000})
                await page.get_by_role(
                    "button", name="使用此模板包快速生成", exact=True
                ).click()
                await page.wait_for_url("**/todo-editor?auto_start=true")
                await page.get_by_text(
                    "生成方式：快速模式（模板包）", exact=True
                ).wait_for()
                await page.wait_for_function(
                    "document.querySelector('#statusMessage').textContent.includes('入口验证完成')"
                )
                assert (
                    stream_requests
                ), "Fast mode did not reach the existing SSE generation entry"
                await page.screenshot(
                    path=str(out / "fast-generator.png"), full_page=True
                )
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.screenshot(
                    path=str(out / "fast-generator-mobile.png"), full_page=True
                )
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                ), "Generator overflows on mobile"
                await page.set_viewport_size({"width": 1440, "height": 1000})
                await page.evaluate(
                    """() => {
                    slidesData = [];
                    currentlyGeneratingPages.clear();
                    updateConnectionStatus('connected', '正在生成');
                    document.getElementById('slidesContainer').innerHTML = '';
                    for (let n = 1; n <= 2; n++) {
                        handleStreamData({type: 'slide', slide_data: {
                            page_number: n, title: `进度验证第 ${n} 页`,
                            html_content: `<html><body><h1>第 ${n} 页</h1></body></html>`
                        }});
                    }
                    // An old preview can survive a failed regeneration; it is not complete.
                    slidesData.push({page_number: 3, html_content: '<html>旧预览</html>'});
                    handleStreamData({type: 'progress', completed: 2, total: 9,
                        message: '正在编写第 3–5 页内容'});
                }"""
                )
                progress_text = await page.locator("#statusMessage").inner_text()
                assert (
                    "2/9 页已完成" in progress_text
                    and "正在编写第 3–5 页内容" in progress_text
                )
                assert "第0页" not in progress_text
                await page.wait_for_function(
                    """() => {
                    const bar = document.getElementById('generationProgress');
                    return Math.abs(bar.getBoundingClientRect().width /
                        bar.parentElement.getBoundingClientRect().width - 2 / 9) < 0.005;
                }"""
                )
                await page.screenshot(
                    path=str(out / "fast-generator-progress.png"), full_page=True
                )
                await page.evaluate(
                    """() => handleStreamData({type: 'complete',
                    partial: true, total: 9, succeeded: 2, message: '部分页面失败，进度已保存'})"""
                )
                width = await page.locator("#generationProgress").evaluate(
                    "el => parseFloat(el.style.width)"
                )
                assert abs(width - 200 / 9) < 0.01, "Partial failure must not show 100%"
                # Exercise the actual EventSource callbacks, including the heartbeat
                # after terminal events, without starting or billing a real generation.
                await page.clock.install()
                await page.evaluate(
                    """() => {
                    window.EventSource = class {
                        static OPEN = 1; static CLOSED = 2; static CONNECTING = 0;
                        constructor(url) { this.url = url; this.readyState = 1; }
                        close() { this.readyState = 2; }
                    };
                    slidesData = [];
                    document.getElementById('slidesContainer').innerHTML = '';
                    for (let n = 1; n <= 9; n++) {
                        if (n === 5) continue;
                        addSlideToContainer({page_number: n, title: `第 ${n} 页`,
                            html_content: `<html><body><h1>第 ${n} 页</h1></body></html>`});
                    }
                }"""
                )
                for terminal in [
                    {
                        "type": "complete",
                        "partial": True,
                        "succeeded": 8,
                        "total": 9,
                        "failed_pages": [5],
                        "message": "第 5 页未能生成，其余页面已保存",
                    },
                    {"type": "error", "message": "第 5 页排版失败，已保存页面不受影响"},
                    {
                        "type": "complete",
                        "partial": False,
                        "succeeded": 9,
                        "total": 9,
                        "message": "模板包页面生成完成",
                    },
                ]:
                    await page.evaluate(
                        """terminal => {
                        isManualStop = false; isPaused = false; generationStarted = true;
                        startEventSourceConnection();
                        const source = eventSource;
                        source.onopen({});
                        source.onmessage({data: JSON.stringify({type: 'progress', completed: 8, total: 9})});
                        attemptReconnect(); // a pending reconnect must also be cancelled
                        source.onmessage({data: JSON.stringify(terminal)});
                        source.onerror({}); // queued callback after the final event
                        source.onmessage({data: JSON.stringify({type: 'progress', completed: 0, total: 9})});
                    }""",
                        terminal,
                    )
                    await page.clock.fast_forward(35000)
                    await page.clock.run_for(500)
                    assert (
                        await page.locator("#statusMessage").inner_text()
                        == terminal["message"]
                    )
                    assert (
                        "重连"
                        not in await page.locator("#connectionStatus").inner_text()
                    )
                    assert await page.evaluate(
                        "heartbeatInterval === null && generationStarted === false"
                    )
                    if terminal.get("partial"):
                        assert await page.get_by_role(
                            "button", name="补生成失败页", exact=True
                        ).is_visible()
                        width = await page.locator("#generationProgress").evaluate(
                            "el => parseFloat(el.style.width)"
                        )
                        assert abs(width - 800 / 9) < 0.01
                        await page.screenshot(
                            path=str(out / "fast-generator-partial.png"), full_page=True
                        )
                await page.goto("http://package.test/editor")
                trigger = page.get_by_role("button", name="内容与版式", exact=True)
                await trigger.wait_for(state="visible")
                assert await trigger.evaluate(
                    "el => el.parentElement.classList.contains('editor-toolbar') && getComputedStyle(el).position !== 'fixed'"
                ), "Package editor action must stay inside the existing toolbar"
                assert await trigger.evaluate(
                    "el => el.previousElementSibling.id === 'quickEditMode'"
                )
                for toolbar_width in (1852, 1440, 1366, 1280):
                    await page.set_viewport_size(
                        {"width": toolbar_width, "height": 1000}
                    )
                    assert await page.locator(".editor-toolbar").evaluate(
                        """toolbar => {
                        const buttons = [...toolbar.querySelectorAll('.toolbar-btn')].filter(b => b.offsetWidth);
                        const top = buttons[0].getBoundingClientRect().top;
                        return buttons.every(b => Math.abs(b.getBoundingClientRect().top - top) < 1)
                            && toolbar.scrollWidth <= toolbar.clientWidth;
                    }"""
                    ), f"Toolbar wraps or overflows at {toolbar_width}px with sidebar"
                await page.set_viewport_size({"width": 1440, "height": 1000})
                await page.screenshot(
                    path=str(out / "editor-toolbar.png"), full_page=True
                )
                await page.set_viewport_size({"width": 390, "height": 844})
                assert await trigger.evaluate(
                    """el => {
                    const button = el.getBoundingClientRect();
                    const toolbar = el.parentElement.getBoundingClientRect();
                    return button.left >= toolbar.left && button.right <= toolbar.right
                        && button.top >= toolbar.top && button.bottom <= toolbar.bottom;
                }"""
                ), "Package button escapes the mobile toolbar"
                await page.screenshot(
                    path=str(out / "editor-toolbar-mobile.png"), full_page=True
                )
                await page.set_viewport_size({"width": 1440, "height": 1000})
                await page.get_by_role("button", name="内容与版式", exact=True).click()
                await page.get_by_label("标题", exact=True).fill(
                    "已经通过浏览器修改的标题"
                )
                await page.screenshot(
                    path=str(out / "content-editor.png"), full_page=True
                )
                await page.get_by_role("button", name="保存内容", exact=True).click()
                await page.wait_for_function(
                    "!document.querySelector('.package-dialog')"
                )
                edited = (await storage.snapshot("smoke"))["pages"][0]
                assert edited["content"]["title"] == "已经通过浏览器修改的标题"
                assert "已经通过浏览器修改的标题" in edited["html_content"]
                package_state_url = "**/api/projects/smoke/template-package"
                await page.route(
                    package_state_url,
                    lambda route: route.fulfill(
                        status=404,
                        content_type="application/json",
                        body='{"detail":"Project does not use a template package"}',
                    ),
                )
                async with page.expect_response(
                    lambda response: response.url.endswith(
                        "/api/projects/smoke/template-package"
                    )
                ):
                    await page.reload()
                assert (
                    not await trigger.is_visible()
                ), "Ordinary projects must not show package editing"
                await page.unroute(package_state_url)
                from template_package_workspace_smoke import check_workspace

                await check_workspace(page, catalog, storage, saved, out)
                assert not errors, errors
                report["ui"] = {
                    "catalog": True,
                    "preview_desktop_mobile": True,
                    "select_package": True,
                    "edit_content": True,
                    "page_errors": errors,
                    "settings_save_and_reload": True,
                    "full_page_mode_picker": True,
                    "generator_auto_start_sse": True,
                    "generator_progress_and_partial_failure": True,
                    "management_tabs_and_keyboard": True,
                    "management_tab_survives_reload": True,
                    "package_editor_uses_toolbar": True,
                    "ai_package_edit_add_modify_delete_publish": True,
                    "ai_package_edit_disconnected_stream": True,
                    "workspace_live_preview_and_multi_round": True,
                    "workspace_page_operations_and_undo": True,
                    "workspace_rename_export_delete": True,
                }
                print("UI and database edit passed", flush=True)
                if ui_only:
                    (out / "ui-report.json").write_text(
                        json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    await browser.close()
                    return

                package = load_builtin_package()
                pages = []
                for component in package.components:
                    content = component.examples[0]
                    result = render_page(
                        package,
                        component.id,
                        content,
                        assets={v.id: VALIDATION_IMAGE for v in content.visual_briefs},
                        allowed_image_urls=frozenset({VALIDATION_IMAGE}),
                    )
                    target = out / f"{component.id}.html"
                    target.write_text(result.html_content, encoding="utf-8")
                    pages.append(
                        {"html_content": result.html_content, "render_mode": "svg"}
                    )
                await page.goto("about:blank")
                await page.evaluate(
                    "window.slidesData=[];window.landpptEditorConfig={renderMode:'svg'}"
                )
                await page.add_script_tag(
                    path="src/landppt/web/static/js/dom-to-pptx.bundle.js"
                )
                await page.add_script_tag(
                    path="src/landppt/web/static/js/pages/project/slides_editor/projectSlidesEditor.svgPptx.js"
                )
                for mode in ("native", "vector"):
                    result = await page.evaluate(
                        """async ({pages,mode}) => {
                        const result = await svgPptx.build(pages, mode, window.domToPptx);
                        const data = await new Promise(resolve => {const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.readAsDataURL(result.blob)});
                        return {data,stats:result.stats};
                    }""",
                        {"pages": pages, "mode": mode},
                    )
                    target = out / f"package-{mode}.pptx"
                    target.write_bytes(base64.b64decode(result["data"]))
                    with zipfile.ZipFile(target) as archive:
                        slides = [
                            n
                            for n in archive.namelist()
                            if n.startswith("ppt/slides/slide") and n.endswith(".xml")
                        ]
                        assert len(slides) == 10
                        if mode == "native":
                            assert all(b"<a:t>" in archive.read(n) for n in slides)
                    report[f"pptx_{mode}"] = {"pages": 10, "stats": result["stats"]}
                    print(f"PPTX {mode}: 10 pages passed", flush=True)
                await browser.close()

        from landppt.services.pyppeteer_pdf_converter import PlaywrightPDFConverter

        converter = PlaywrightPDFConverter()
        try:
            pdfs = []
            for component in package.components:
                target = out / f"{component.id}.pdf"
                assert await converter.html_to_pdf(
                    str(out / f"{component.id}.html"), str(target)
                )
                with fitz.open(target) as document:
                    assert len(document) == 1
                    assert document[0].get_text().strip()
                pdfs.append(str(target))
            assert await converter.merge_pdfs(pdfs, str(out / "package.pdf"))
            report["pdf"] = {"pages": 10, "searchable_text": True}
            print("PDF: 10 searchable pages passed", flush=True)
            from landppt.services.video_export_service import (
                NarrationVideoExportService,
            )

            video_html = NarrationVideoExportService()._build_live_single_slide_html(
                (out / "cover.html").read_text(encoding="utf-8"), width=1280, height=720
            )
            (out / "video.html").write_text(video_html, encoding="utf-8")
            assert await converter.record_html_video(
                str(out / "video.html"),
                str(out / "cover.webm"),
                width=1280,
                height=720,
                duration_ms=1000,
            )
            report["video"] = {
                "render_recording": True,
                "audio_synthesis_tested": False,
            }
            print("Video rendering passed (silent fixture)", flush=True)
        finally:
            await converter.close()
        (out / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Smoke artifacts: {out}", flush=True)
    finally:
        decision_patch.stop()
        config_patch.stop()
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ui-only",
        action="store_true",
        help="Verify settings and generation entry points without rerunning exports",
    )
    asyncio.run(main(parser.parse_args().ui_only))
