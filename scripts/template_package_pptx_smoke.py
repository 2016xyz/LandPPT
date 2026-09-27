"""Real isolated LibreOffice + browser + SQLite import/publish/ZIP smoke.

Set LANDPPT_PPTX_RENDER_SPOOL and start docker-compose-pptx-renderer.yaml first.
Uses a generated fixture, an isolated database and no model calls or user data.
"""

import asyncio
import importlib.util
import json
import socket
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from playwright.async_api import async_playwright, expect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from landppt.api import template_package_api as api
from landppt.api import template_package_import_api as imports
from landppt.database.models import Base, User
from landppt.services.template_package.archive import import_archive
from landppt.services.template_package.catalog import PackageCatalog


async def main():
    out = Path("artifacts/pptx-import")
    out.mkdir(parents=True, exist_ok=True)
    fixture_spec = importlib.util.spec_from_file_location(
        "pptx_fixture", "tests/test_template_package_pptx_import.py"
    )
    fixture = importlib.util.module_from_spec(fixture_spec)
    fixture_spec.loader.exec_module(fixture)
    source = out / "source.pptx"
    source.write_bytes(fixture.fixture_pptx())
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{out / ('smoke-' + uuid.uuid4().hex + '.db')}"
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with sessions() as session, session.begin():
        session.add(User(id=1, username="pptx-smoke", password_hash="unused"))
    catalog = PackageCatalog(1, sessions)
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(imports.router)
    app.dependency_overrides[api.catalog] = lambda: catalog
    app.dependency_overrides[api.get_current_user_required] = lambda: SimpleNamespace(
        id=1
    )
    app.mount("/static", StaticFiles(directory="src/landppt/web/static"))
    env = Environment(
        loader=FileSystemLoader("src/landppt/web/templates"), autoescape=True
    )

    @app.get("/", response_class=HTMLResponse)
    async def import_page():
        return (
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>body{font-family:sans-serif;margin:32px}*{box-sizing:border-box}</style>'
            + env.get_template("components/template/package_panel.html").render(
                package_panel_mode="manage"
            )
            + "</html>"
        )

    server_socket = socket.socket()
    server_socket.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    serving = asyncio.create_task(server.serve(sockets=[server_socket]))
    while not server.started:
        await asyncio.sleep(0.05)
    origin = f"http://127.0.0.1:{server_socket.getsockname()[1]}"

    async def mock_analysis(result, service, progress):
        progress("ai", "测试使用固定分析结果，不调用模型")
        return result

    try:
        with (
            patch.object(
                imports, "PackageCatalog", lambda uid: PackageCatalog(uid, sessions)
            ),
            patch.object(imports, "analysis_service", lambda uid: None),
            patch.object(imports, "classify_analysis", mock_analysis),
        ):
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://package.test",
                    timeout=300,
                ) as client,
                async_playwright() as pw,
            ):
                browser = await pw.chromium.launch()
                context = await browser.new_context(
                    viewport={"width": 1440, "height": 1050}
                )
                errors = []
                page = await context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(origin)
                await page.get_by_role(
                    "button", name="从 PPTX 创建", exact=True
                ).click()
                wizard = page.locator(".pptx-import")
                await wizard.locator('input[type="file"]').set_input_files(str(source))
                await wizard.get_by_role("button", name="分析页面", exact=True).click()
                await page.screenshot(
                    path=str(out / "analyze-start.png"), full_page=True
                )
                await expect(wizard.locator(".pptx-page")).to_have_count(
                    3, timeout=240000
                )
                await wizard.locator("summary").first.click()
                await wizard.locator(".pptx-page").first.locator(
                    ".pptx-binding"
                ).filter(has_text="Picture").locator("select").select_option("image")
                await page.screenshot(
                    path=str(out / "import-desktop.png"), full_page=True
                )
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.screenshot(
                    path=str(out / "import-mobile.png"), full_page=True
                )
                await page.set_viewport_size({"width": 1440, "height": 1050})
                await wizard.get_by_role(
                    "button", name="生成模板包草稿", exact=True
                ).click()
                await expect(
                    wizard.get_by_role("button", name="打开草稿，对照原稿", exact=True)
                ).to_be_visible(timeout=300000)
                await wizard.get_by_role(
                    "button", name="打开草稿，对照原稿", exact=True
                ).click()
                workspace = page.locator(".package-workspace")
                await workspace.frame_locator(".pw-stage iframe").locator(
                    "svg"
                ).wait_for()
                await workspace.get_by_role(
                    "button", name="对照原稿", exact=True
                ).click()
                await page.locator(".pptx-compare iframe").wait_for()
                await page.locator(".pptx-compare img").evaluate("img => img.decode()")
                await page.locator(".pptx-compare").evaluate(
                    "el => { const boxes=[...el.children].map(n=>n.getBoundingClientRect()); if(boxes.some(b=>b.width<300) || Math.abs(boxes[0].width-boxes[1].width)>2) throw new Error('Comparison columns are not balanced'); }"
                )
                await page.screenshot(path=str(out / "comparison.png"), full_page=True)
                await page.locator("dialog").last.get_by_role(
                    "button", name="关闭", exact=True
                ).click()
                await workspace.get_by_role(
                    "button", name="校验并发布", exact=True
                ).click()
                await expect(workspace.locator(".pw-meta")).to_contain_text(
                    "已发布", timeout=30000
                )
                saved = (await catalog.list())[0]
                archive = await client.get(
                    f"/api/global-master-templates/packages/{saved['id']}/export"
                )
                assert (
                    archive.status_code == 200
                    and "zip" in archive.headers["content-type"]
                )
                (out / "package.zip").write_bytes(archive.content)
                restored = import_archive(archive.content)
                assert restored.content_hash() == saved["content_hash"]
                response = await client.post(
                    "/api/global-master-templates/packages-import",
                    files={"file": ("package.zip", archive.content, "application/zip")},
                )
                assert response.status_code == 200, response.text
                assert response.json()["status"] == "draft"
                assert not errors, errors
                report = {
                    "components": len(restored.components),
                    "examples_per_component": [
                        len(c.examples) for c in restored.components
                    ],
                    "published": saved["status"],
                    "zip_roundtrip": True,
                    "browser_errors": errors,
                    "uses_live_models": False,
                }
                (out / "smoke-report.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(json.dumps(report))
                await browser.close()
    finally:
        server.should_exit = True
        await serving
        server_socket.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
