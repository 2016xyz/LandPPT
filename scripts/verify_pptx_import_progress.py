"""Browser regression: import status stays visible below a long page list.

Uses mocked import streams against the local development UI; saves no drafts.
"""

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright, expect


async def check(browser, width, height, output):
    context = await browser.new_context(
        base_url="http://localhost:8000", viewport={"width": width, "height": height}
    )
    response = await context.request.post(
        "/auth/login", form={"username": "admin", "password": "admin123"}
    )
    assert response.ok
    page = await context.new_page()
    await page.goto("/global-master-templates", wait_until="domcontentloaded")
    await page.locator("#packageTemplatesTab").click()
    await page.locator("#packagePptxImport").click()
    await page.evaluate(
        """() => {
        const original = window.fetch;
        window.fetch = (url, options) => {
            if (!String(url).includes('/packages-pptx/')) return original(url, options);
            const encoder = new TextEncoder();
            return Promise.resolve(new Response(new ReadableStream({start(controller) {
                const emit = event => controller.enqueue(encoder.encode(JSON.stringify(event)+'\\n'));
                if (String(url).includes('/analyze')) {
                    const slides = Array.from({length: 10}, (_, i) => ({
                        slide: i + 1, family: 'cover', warnings: [],
                        preview: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90"><rect width="160" height="90" fill="white"/></svg>',
                        candidates: [{id: '1:0', kind: 'text', text: '测试标题', suggested: 'title', capacity: 20}]
                    }));
                    emit({type: 'complete', result: {slides, warnings: []}});
                    controller.close();
                } else {
                    window.emitImportProgress = emit;
                    window.finishImport = event => { emit(event); controller.close(); };
                    emit({type: 'progress', stage: 'background', message: '正在渲染 10 页清除示例文字后的底图'});
                }
            }}), {headers: {'Content-Type': 'application/x-ndjson'}}));
        };
        }"""
    )
    modal = page.locator("dialog.pptx-import")
    await modal.locator('input[type="file"]').set_input_files(
        {
            "name": "progress-test.pptx",
            "mimeType": "application/octet-stream",
            "buffer": b"mock",
        }
    )
    await modal.get_by_role("button", name="分析页面", exact=True).click()
    await expect(modal.locator("details")).to_have_count(10)
    await modal.evaluate("el => el.scrollTop = el.scrollHeight")
    await modal.get_by_role("button", name="生成模板包草稿", exact=True).click()
    await expect(
        modal.get_by_role("button", name="正在生成…", exact=True)
    ).to_be_disabled()
    status = modal.locator(".package-status")
    await expect(status).to_contain_text("正在渲染 10 页")

    async def assert_visible_in_viewport():
        box = await status.bounding_box()
        assert box and box["y"] >= 0 and box["y"] + box["height"] <= height
        assert box["x"] >= 0 and box["x"] + box["width"] <= width
        assert await status.evaluate(
            "el => { const r=el.getBoundingClientRect(); return el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)); }"
        )

    for position in (1, 0, 0.5, 1):
        await modal.evaluate(
            "(el, ratio) => el.scrollTop = el.scrollHeight * ratio", position
        )
        await assert_visible_in_viewport()
        await expect(modal.locator("progress")).to_be_visible()
    await page.evaluate(
        "window.emitImportProgress({type:'progress',stage:'repair',message:'第 3 页 AI 正在第 1/2 次修复槽位'})"
    )
    await expect(status).to_contain_text("第 3 页 AI")
    await expect(status).to_contain_text("已用时 1 秒", timeout=5000)
    await assert_visible_in_viewport()
    await page.screenshot(path=str(output / f"progress-{width}.png"), full_page=True)
    await page.evaluate(
        "window.finishImport({type:'error',message:'测试错误：修复未通过校验'})"
    )
    await expect(status).to_contain_text("测试错误")
    await expect(modal.locator("progress")).to_be_hidden()
    await expect(
        modal.get_by_role("button", name="生成模板包草稿", exact=True)
    ).to_be_enabled()
    await assert_visible_in_viewport()
    await modal.get_by_role("button", name="生成模板包草稿", exact=True).click()
    await expect(status).to_contain_text("正在渲染 10 页")
    await page.evaluate(
        "window.finishImport({type:'complete',result:{warnings:[],failures:[],partial:false,package:{manifest:{components:[{}]}}}})"
    )
    await expect(status).to_contain_text("已保存 1 个版式")
    await expect(modal.get_by_role("button", name="打开草稿，对照原稿")).to_be_visible()
    await assert_visible_in_viewport()
    await context.close()


async def main():
    output = Path("artifacts/pptx-import-progress")
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            await check(browser, 1440, 900, output)
            await check(browser, 390, 844, output)
        finally:
            await browser.close()
    print(
        "PASS: desktop/mobile, scroll positions, repair, timer, error, retry and completion; mocked streams, no drafts saved"
    )


if __name__ == "__main__":
    asyncio.run(main())
