"""Browser workspace checks used by template_package_smoke.py; no live models."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from landppt.services.template_package.builtin import load_builtin_package
from landppt.services.template_package.editor import ContentService, preview_html
from landppt.services.template_package.schemas import TemplatePackage


async def check_workspace(page, catalog, storage, saved, out):
    await page.goto("http://package.test/global-master-templates")
    await page.get_by_role("tab", name="快速模式模板包", exact=True).click()
    await page.get_by_role("button", name="编辑", exact=True).click()
    workspace = page.locator(".package-workspace")
    await workspace.frame_locator(".pw-stage iframe").locator("svg").wait_for()

    # A real browser ReadableStream pauses after a component, before completion.
    # This tests immediate preview and removal of unsaved additions on failure.
    package = load_builtin_package()
    html = preview_html(package, package.components[0]).replace(
        "<body", '<body data-live="yes"'
    )
    event = {
        "type": "component",
        "action": "add",
        "component_id": "temporary_page",
        "family": "cover",
        "description": "临时预览",
        "html": html,
    }
    await page.evaluate(
        """item => {
        window.originalWorkspaceFetch = window.fetch;
        window.fetch = (url, options) => {
            if (!String(url).endsWith('/edit')) return window.originalWorkspaceFetch(url, options);
            return Promise.resolve(new Response(new ReadableStream({start(controller) {
                controller.enqueue(new TextEncoder().encode('data: ' + JSON.stringify(item) + '\\n\\n'));
                window.releaseWorkspaceStream = () => controller.close();
            }}), {headers: {'Content-Type': 'text/event-stream'}}));
        };
    }""",
        event,
    )
    await page.get_by_label("整套模板修改要求").fill("新增临时预览")
    await workspace.get_by_role("button", name="发送", exact=True).click()
    await workspace.frame_locator(".pw-stage iframe").locator(
        'body[data-live="yes"]'
    ).wait_for()
    assert await workspace.locator('[data-id="temporary_page"]').count() == 1
    assert await workspace.get_by_role("button", name="发送", exact=True).is_disabled()
    await page.evaluate(
        "window.releaseWorkspaceStream(); window.fetch=window.originalWorkspaceFetch"
    )
    await workspace.locator(".pw-status").filter(has_text="连接已中断").wait_for()
    assert await workspace.locator('[data-id="temporary_page"]').count() == 0
    drafts = [p for p in await catalog.list() if p["editable"]]
    assert len(drafts) == 1 and len(drafts[0]["manifest"]["components"]) == 10
    draft_id = drafts[0]["id"]

    def op(action, cid, **kw):
        return {"action": action, "component_id": cid, "instruction": "调整页面", **kw}

    completion = AsyncMock(
        side_effect=[
            (
                {
                    "operations": [
                        op("modify", "cover"),
                        op("delete", "section"),
                        op("add", "new_points", reference_id="points_3"),
                    ]
                },
                {},
            ),
            ({"changes": {"description": "第一轮封面"}}, {}),
            ({"changes": {"description": "新要点页面"}}, {}),
            ({"operations": [op("modify", "new_points")]}, {}),
            ({"changes": {"description": "第二轮改进的要点页面"}}, {}),
            ({"changes": {"description": "单页修改后的封面"}}, {}),
            ({"changes": {"description": "单独新增的页面"}}, {}),
        ]
    )
    provider = SimpleNamespace(
        user_id=1,
        get_role_provider_async=AsyncMock(return_value=(None, {"provider": "openai"})),
    )
    with (
        patch.object(ContentService, "json_completion", completion),
        patch(
            "landppt.web.route_modules.support.get_ppt_service_for_user",
            return_value=provider,
        ),
        patch(
            "landppt.web.route_modules.support.check_credits_for_operation",
            AsyncMock(return_value=(True, 0, 0)),
        ),
        patch(
            "landppt.web.route_modules.support.consume_credits_for_operation",
            AsyncMock(return_value=(True, "ok")),
        ),
    ):

        async def wait_idle():
            await page.wait_for_function(
                "!document.querySelector('.package-workspace').classList.contains('is-busy')"
            )

        for prompt in (
            "修改封面、删除章节页、增加要点页面",
            "继续改进刚才新增的要点页面",
        ):
            await page.get_by_label("整套模板修改要求").fill(prompt)
            await workspace.get_by_role("button", name="发送", exact=True).click()
            await page.wait_for_function(
                "document.querySelector('.pw-chat-form textarea').value === ''"
            )
            await wait_idle()
        draft = await catalog.get(draft_id)
        assert len([p for p in await catalog.list() if p["editable"]]) == 1
        assert draft["version"] == 2
        assert "继续改进" in completion.await_args.args[0]
        assert any(
            c["description"] == "第二轮改进的要点页面"
            for c in draft["manifest"]["components"]
        )
        await workspace.locator('[data-id="cover"] button').click()
        await page.get_by_label("此页修改要求").fill("进一步改进封面")
        await workspace.get_by_role("button", name="AI 修改此页", exact=True).click()
        await workspace.locator(".pw-status").filter(has_text="此页已更新").wait_for()
        await wait_idle()
        await workspace.get_by_role("button", name="+ 新增页面", exact=True).click()
        await page.get_by_label("参考版式", exact=True).select_option("points_3")
        await page.get_by_label("新页面要求").fill("新增三要点版式")
        await workspace.get_by_role("button", name="AI 生成页面", exact=True).click()
        await workspace.locator(".pw-status").filter(has_text="新页面已添加").wait_for()
        await wait_idle()
        assert await workspace.locator(".pw-page").count() == 11
        await workspace.get_by_role("button", name="复制", exact=True).click()
        await workspace.locator(".pw-status").filter(has_text="已复制页面").wait_for()
        await wait_idle()
        assert await workspace.locator(".pw-page").count() == 12
        await workspace.get_by_role("button", name="上移", exact=True).click()
        await workspace.locator(".pw-status").filter(has_text="已上移").wait_for()
        await wait_idle()
        await workspace.get_by_role("button", name="删除", exact=True).click()
        await page.locator(".package-confirm").get_by_role(
            "button", name="删除", exact=True
        ).click()
        await workspace.locator(".pw-status").filter(has_text="已删除页面").wait_for()
        await wait_idle()
        assert await workspace.locator(".pw-page").count() == 11
        await workspace.get_by_role("button", name="撤销", exact=True).click()
        await workspace.locator(".pw-status").filter(has_text="已撤销").wait_for()
        await wait_idle()
        assert await workspace.locator(".pw-page").count() == 12
        await workspace.get_by_role("button", name="重命名", exact=True).click()
        await page.get_by_label("模板包名称").fill("多轮编辑验证包")
        await page.get_by_label("模板包名称").press("Enter")
        await workspace.locator(".pw-status").filter(has_text="已重命名").wait_for()
        async with page.expect_download() as info:
            await workspace.get_by_role("button", name="导出 JSON", exact=True).click()
        download = await info.value
        await download.save_as(out / "workspace-export.json")
        exported = TemplatePackage.model_validate_json(
            (out / "workspace-export.json").read_text(encoding="utf-8")
        )
        assert exported.name == "多轮编辑验证包" and len(exported.components) == 12
        assert await workspace.locator(".pw-page").first.evaluate(
            "el => el.getBoundingClientRect().width <= el.closest('.pw-rail').clientWidth"
        )
        assert await workspace.locator(".pw-thumb").first.evaluate(
            "el => Math.abs(el.clientHeight - el.clientWidth * 9 / 16) < 2"
        )
        await page.screenshot(path=str(out / "package-workspace-desktop.png"))
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await workspace.evaluate("el => el.scrollWidth <= el.clientWidth")
        assert await workspace.evaluate(
            "el => el.querySelector('.pw-stage').getBoundingClientRect().bottom <= el.querySelector('.pw-chat').getBoundingClientRect().top + 1"
        )
        await page.screenshot(path=str(out / "package-workspace-mobile.png"))
        await page.set_viewport_size({"width": 1440, "height": 1000})
        await workspace.get_by_role("button", name="校验并发布", exact=True).click()
        await workspace.locator(".pw-status").filter(has_text="v2 已发布").wait_for()
        assert (await catalog.get(saved["id"]))["manifest"] == saved["manifest"]
        assert (await storage.snapshot("smoke"))["version_id"] == saved["id"]
        await workspace.get_by_role("button", name="关闭", exact=True).click()

    await page.locator(".package-card").first.get_by_text("更多", exact=True).click()
    await page.locator(".package-card").first.get_by_role(
        "button", name="删除模板包", exact=True
    ).click()
    await page.locator(".package-confirm").get_by_role(
        "button", name="删除模板包", exact=True
    ).click()
    await page.locator("#packageStatus").filter(has_text="已删除").wait_for()
    assert await catalog.list() == []
    assert (await storage.snapshot("smoke"))["version_id"] == saved["id"]
