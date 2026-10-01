"""AI editing controls must stay inside the workspace without scrolling."""

from pathlib import Path

import pytest

STATIC = Path("src/landppt/web/static")
OUT = Path("artifacts/template-package-workspace-layout")
VIEWPORTS = [
    (1680, 888),
    (1440, 900),
    (1366, 768),
    (1280, 600),
    (1024, 768),
    (900, 600),
    (844, 390),
    (700, 760),
    (390, 844),
    (390, 667),
    (320, 568),
]


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runner:
        try:
            instance = runner.chromium.launch()
        except Exception as exc:
            pytest.skip(f"Chromium is not available: {exc}")
        yield instance
        instance.close()


@pytest.fixture
def workspace_page(browser):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route(
        "**/*",
        lambda route: route.fulfill(
            content_type="text/html; charset=utf-8",
            body="""<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
                <style>body { font-family: sans-serif; margin: 0; } svg { width:100%; }</style>
                <svg viewBox="0 0 1280 720" xmlns="http://www.w3.org/2000/svg">
                <rect width="1280" height="720" fill="#102038"/>
                <text x="80" y="180" fill="white" font-size="56">Template preview</text>
                <rect x="80" y="240" width="1120" height="4" fill="#00bfd8"/>
                </svg>""",
        ),
    )
    page.goto("http://workspace.test/")
    page.add_style_tag(path=str(STATIC / "css/template-packages.css"))
    page.add_script_tag(path=str(STATIC / "js/template-packages.js"))
    page.add_script_tag(path=str(STATIC / "js/template-package-workspace.js"))
    component = {
        "family": "cover",
        "description": "原稿第 1 页：4 个要点、小标题和说明，包含配图与副标题。" * 20,
        "blocks": {"minimum": 4, "maximum": 4},
        "metrics": {"minimum": 0, "maximum": 0},
        "images": {"minimum": 1, "maximum": 1},
    }
    package = {
        "id": 1,
        "template_id": 1,
        "template_name": "LandPPT_dom-to-pptx",
        "version": 1,
        "status": "published",
        "editable": False,
        "user_id": 1,
        "content_hash": "a" * 64,
        "manifest": {
            "components": [{**component, "id": f"slide-{i}"} for i in range(1, 7)]
        },
    }
    yield page, package, errors
    page.close()


def assert_controls_fit(page):
    page.wait_for_function(
        """() => {
            const preview = document.querySelector('.pw-stage-preview');
            const area = document.querySelector('.pw-preview-area');
            return preview?.clientWidth > 0 &&
                Math.abs(preview.clientWidth - Math.min(area.clientWidth, area.clientHeight * 16 / 9)) < 2;
        }"""
    )
    # Bounding boxes and hit testing detect clipping; Playwright's click/visibility
    # helpers would scroll an offscreen control into view and hide the regression.
    results = page.evaluate(
        """() => {
            const workspace = document.querySelector('.package-workspace');
            const body = workspace.querySelector('.pw-body');
            const bounds = body.getBoundingClientRect();
            const selectors = ['.pw-page-ai input', '.pw-page-ai button',
                '.pw-chat-form textarea', '.pw-chat-form button'];
            return selectors.map(selector => {
                const el = workspace.querySelector(selector);
                const box = el.getBoundingClientRect();
                const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
                return {selector, fits: box.top >= bounds.top - 1 && box.bottom <= bounds.bottom + 1 &&
                    box.left >= bounds.left - 1 && box.right <= bounds.right + 1,
                    reachable: hit === el || el.contains(hit)};
            });
        }"""
    )
    assert all(result["fits"] and result["reachable"] for result in results), results
    assert page.locator(".pw-body").evaluate("el => el.scrollTop") == 0
    assert page.locator(".pw-stage").evaluate("el => el.scrollTop") == 0
    assert page.locator(".package-workspace").evaluate(
        "el => el.scrollHeight <= el.clientHeight + 1 && el.scrollWidth <= el.clientWidth + 1"
    )
    preview = page.locator(".pw-stage-preview").bounding_box()
    assert preview["height"] > 10
    assert abs(preview["width"] / preview["height"] - 16 / 9) < 0.04


@pytest.mark.parametrize("width,height", VIEWPORTS)
def test_ai_controls_are_visible_without_scrolling(workspace_page, width, height):
    page, package, errors = workspace_page
    page.set_viewport_size({"width": width, "height": height})
    page.evaluate("pkg => window.PackageWorkspace.open(pkg)", package)
    assert_controls_fit(page)
    page.get_by_label("此页修改要求").fill("放大标题，改为左右分栏")
    page.get_by_label("整套模板修改要求").fill("整体改成深蓝配色")
    assert_controls_fit(page)
    OUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUT / f"workspace-{width}x{height}.png"))
    # Many rounds and a long status must only scroll their own content, while
    # both editing forms remain available in the same place.
    page.evaluate(
        """() => {
            const log = document.querySelector('.pw-log');
            log.replaceChildren(...Array.from({length: 30}, (_, i) => {
                const item = document.createElement('li');
                item.textContent = `第 ${i + 1} 轮：` + '修改封面并新增要点页面。'.repeat(8);
                return item;
            }));
            document.querySelector('.pw-status').textContent = '已保存到草稿。'.repeat(30);
        }"""
    )
    assert_controls_fit(page)
    assert not errors, errors


def test_preview_and_controls_follow_window_resizing(workspace_page):
    page, package, errors = workspace_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.evaluate("pkg => window.PackageWorkspace.open(pkg)", package)
    for width, height in ((1440, 900), (900, 600), (390, 844), (1366, 768)):
        page.set_viewport_size({"width": width, "height": height})
        assert_controls_fit(page)
    assert not errors, errors


def test_multi_round_reply_streams_before_save_and_keeps_partial_text_on_error(
    workspace_page,
):
    page, package, errors = workspace_page
    page.set_viewport_size({"width": 1440, "height": 900})
    package.update(editable=True, status="draft", version=2)
    page.evaluate(
        """pkg => {
            window.editRequests = [];
            const original = window.fetch;
            window.fetch = (url, options) => {
                if (!String(url).endsWith('/edit')) return original(url, options);
                window.editRequests.push(JSON.parse(options.body));
                return Promise.resolve(new Response(new ReadableStream({start(controller) {
                    window.sendEditEvent = event => {
                        const bytes = new TextEncoder().encode('data: ' + JSON.stringify(event) + '\\n\\n');
                        // Network chunks may split a UTF-8 character or an SSE record.
                        for (let i = 0; i < bytes.length; i += 7) controller.enqueue(bytes.slice(i, i + 7));
                    };
                    window.closeEditStream = () => controller.close();
                }}), {headers: {'Content-Type': 'text/event-stream'}}));
            };
            window.PackageWorkspace.open(pkg);
        }""",
        package,
    )

    def send(event):
        page.evaluate("event => window.sendEditEvent(event)", event)

    def submit(prompt):
        page.get_by_label("整套模板修改要求").fill(prompt)
        before = page.evaluate("window.editRequests.length")
        page.get_by_role("button", name="发送", exact=True).click()
        page.wait_for_function(
            "count => window.editRequests.length > count", arg=before
        )

    submit("添加配图页")
    send({"type": "progress", "message": "正在规划版式修改…"})
    send({"type": "reply", "segment_id": "plan", "text": "", "reset": True})
    send({"type": "reply", "segment_id": "plan", "text": "我会新增"})
    page.wait_for_function(
        "document.querySelector('.pw-round-reply').textContent === '我会新增'"
    )
    assert page.get_by_role("button", name="发送", exact=True).is_disabled()
    assert page.locator(".pw-round-note").text_content() == "正在规划版式修改…"
    assert_controls_fit(page)
    send({"type": "reply", "segment_id": "plan", "text": "图文页，保留标题。"})
    page.wait_for_function(
        "document.querySelector('.pw-round-reply').textContent === '我会新增图文页，保留标题。'"
    )
    OUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUT / "workspace-streaming-reply.png"))
    send(
        {
            "type": "reply",
            "segment_id": "component:new-image",
            "text": "",
            "reset": True,
        }
    )
    send(
        {
            "type": "reply",
            "segment_id": "component:new-image",
            "text": "左侧配图，右侧放置要点。",
        }
    )
    page.wait_for_function(
        "document.querySelector('.pw-round-reply').textContent.includes('左侧配图')"
    )
    updated = {**package, "content_hash": "b" * 64}
    send(
        {
            "type": "complete",
            "package": updated,
            "changes": [],
            "message": "修改已保存到草稿。",
        }
    )
    page.wait_for_function(
        "!document.querySelector('.package-workspace').classList.contains('is-busy')"
    )
    assert page.locator(".pw-round-note").text_content() == "已保存到草稿。"
    assert page.locator(".pw-round.is-streaming").count() == 0
    assert "左侧配图" in page.locator(".pw-round-reply").text_content()
    assert page.get_by_label("整套模板修改要求").input_value() == ""

    submit("继续调整配图页")
    assert page.evaluate("window.editRequests[1].history") == ["添加配图页"]
    send(
        {
            "type": "reply",
            "segment_id": "plan",
            "text": "正在调整配图区域。",
            "reset": True,
        }
    )
    page.wait_for_function(
        "document.querySelectorAll('.pw-round-reply')[1].textContent === '正在调整配图区域。'"
    )
    send(
        {
            "type": "component",
            "action": "add",
            "component_id": "temporary-page",
            "family": "image",
            "description": "未保存的预览",
            "html": "<svg></svg>",
        }
    )
    page.locator('[data-id="temporary-page"]').wait_for()
    send({"type": "error", "message": "版式未通过校验，未保存任何修改。"})
    page.wait_for_function(
        "!document.querySelector('.package-workspace').classList.contains('is-busy')"
    )
    assert page.locator('[data-id="temporary-page"]').count() == 0
    assert (
        page.locator(".pw-round.is-error .pw-round-reply").text_content()
        == "正在调整配图区域。"
    )
    assert (
        "未保存任何修改"
        in page.locator(".pw-round.is-error .pw-round-note").text_content()
    )
    assert page.get_by_label("整套模板修改要求").input_value() == "继续调整配图页"
    assert_controls_fit(page)
    assert not errors, errors
