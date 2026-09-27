"""Local browser smoke against a running dev app; deletes only its own test draft."""

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright, expect


async def main():
    out = Path("artifacts/pptx-ai-import")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(
            base_url="http://localhost:8000", viewport={"width": 1440, "height": 1000}
        )
        response = await context.request.post(
            "/auth/login", form={"username": "admin", "password": "admin123"}
        )
        assert response.ok
        page = await context.new_page()
        await page.goto("/global-master-templates", wait_until="domcontentloaded")
        await page.locator("#packageTemplatesTab").click()
        await page.locator("#packagePptxImport").click()
        modal = page.locator("dialog.pptx-import")
        await modal.locator('input[type="file"]').set_input_files(out / "source.pptx")
        await modal.get_by_role("button", name="分析页面", exact=True).click()
        await expect(modal.locator('.pptx-stages [data-stage="ai"]')).to_have_attribute(
            "data-state", "active", timeout=180000
        )
        await page.screenshot(path=str(out / "ai-progress.png"), full_page=True)
        await expect(
            modal.get_by_role("button", name="生成模板包草稿", exact=True)
        ).to_be_visible(timeout=180000)
        await modal.locator("details summary").click()
        await expect(modal.locator(".pptx-ai-reason").first).to_be_visible()
        await page.screenshot(path=str(out / "ai-result.png"), full_page=True)
        async with page.expect_response(
            lambda r: "/packages-pptx/import?" in r.url, timeout=180000
        ) as pending:
            await modal.get_by_role("button", name="生成模板包草稿", exact=True).click()
        response = await pending.value
        events = [json.loads(line) for line in (await response.text()).splitlines()]
        complete = next(e for e in events if e["type"] == "complete")
        saved = complete["result"]["package"]
        try:
            assert not complete["result"]["failures"]
            await expect(
                modal.get_by_role("button", name="打开草稿，对照原稿")
            ).to_be_visible(timeout=30000)
            persisted = await context.request.get(
                f"/api/global-master-templates/packages/{saved['id']}"
            )
            assert persisted.ok
            assert (await persisted.json())["content_hash"] == saved["content_hash"]
            await page.screenshot(path=str(out / "import-saved.png"), full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.screenshot(path=str(out / "import-mobile.png"), full_page=True)
            (out / "browser-report.json").write_text(
                json.dumps(
                    {
                        "saved_id": saved["id"],
                        "status": saved["status"],
                        "phases": [
                            e["stage"] for e in events if e["type"] == "progress"
                        ],
                        "persisted_hash_verified": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        finally:
            # Only the known draft created by this smoke run is removed.
            response = await context.request.delete(
                f"/api/global-master-templates/packages/{saved['id']}"
            )
            assert response.ok
        await browser.close()
    print(
        "PASS: browser progress, AI groups, real draft persistence, mobile screenshot; test draft removed"
    )


if __name__ == "__main__":
    asyncio.run(main())
