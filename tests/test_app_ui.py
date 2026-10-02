"""Browser regressions for navigation, bounded profile layout and modal focus.

Render actual frontend files with fixture GET responses; no app/database or AI.
"""

import json
import mimetypes
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/landppt/web/static"


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        try:
            instance = runtime.chromium.launch()
        except Exception as exc:
            pytest.skip(f"Chromium is not available: {exc}")
        yield instance
        instance.close()


@pytest.fixture
def ui_page(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    env = Environment(
        loader=FileSystemLoader(ROOT / "src/landppt/web/templates"), autoescape=True
    )
    env.filters["strftime"] = lambda value, fmt: value.strftime(fmt)
    user = dict(
        id=1,
        username="ui-test",
        is_admin=True,
        is_active=True,
        email="long-email-" * 20 + "@example.invalid",
        created_at="2026-10-02",
    )
    project = dict(
        project_id="ui-test",
        title="示例项目",
        topic="布局与键盘测试",
        scenario="general",
        status="completed",
        todo_board=None,
        updated_at=datetime(2026, 10, 2),
        project_metadata={},
        outline={"slides": [{"title": "封面", "content": []}]},
        slides_data=[
            dict(
                slide_id="slide-1",
                slide_number=1,
                title="封面",
                render_mode="html",
                html_content='<html><body style="margin:0;width:1280px;height:720px;background:#17263e">Original slide</body></html>',
            )
        ],
    )
    state = {"html": ""}

    def forward(route):
        path = unquote(urlsplit(route.request.url).path)
        if "bootstrap@5.1.3/dist/css/bootstrap.min.css" in route.request.url:
            # Keep the CDN reset and utility precedence in the isolated editor
            # fixture; these declarations also apply in the live browser.
            route.fulfill(
                body="*,::before,::after{box-sizing:border-box}.d-flex{display:flex!important}.align-items-center{align-items:center!important}",
                content_type="text/css",
            )
        elif route.request.url.startswith("https:"):
            route.abort()
        elif path.startswith("/static/"):
            source = STATIC / path.removeprefix("/static/")
            if source.is_file() and source.resolve().is_relative_to(STATIC.resolve()):
                mime = (
                    mimetypes.guess_type(str(source))[0] or "application/octet-stream"
                )
                route.fulfill(body=source.read_bytes(), content_type=mime)
            else:
                route.fulfill(status=404, body="")
        elif route.request.resource_type == "document":
            route.fulfill(body=state["html"], content_type="text/html")
        else:
            assert route.request.method == "GET", route.request.url
            route.fulfill(
                body=json.dumps(
                    {
                        "success": True,
                        "enabled": False,
                        "data": [],
                        "config": {},
                        "notifications": [],
                        "unread_count": 0,
                        "templates": [],
                        "packages": [],
                        "images": [],
                        "tags": [],
                        "users": [],
                        "total": 0,
                        "page": 1,
                        "total_pages": 1,
                        "pagination": {
                            "current_page": 1,
                            "total_pages": 1,
                            "total_count": 0,
                        },
                    }
                ),
                content_type="application/json",
            )

    context.route("**/*", forward)

    def load(template, path="/projects", **overrides):
        values = dict(
            user=user,
            request=SimpleNamespace(state=SimpleNamespace(user=user)),
            credits_enabled=True,
            project=project,
            projects=[project],
            recent_projects=[project],
            total=1,
            page=1,
            page_size=20,
            status_filter=None,
            current_provider="openai",
            current_config={},
            available_providers=[],
            provider_status={},
            registration_enabled=True,
            active_tab="login",
            scenarios=[],
            unattended_defaults=None,
            template_options=[],
            narration_video_tools_enabled=False,
        )
        values.update(overrides)
        values["request"] = SimpleNamespace(state=SimpleNamespace(user=values["user"]))
        state["html"] = env.get_template(template).render(**values)
        page.goto("http://ui.test" + path, wait_until="networkidle")
        return page

    yield page, load
    context.close()


@pytest.mark.parametrize("width,height", [(390, 844), (320, 568), (844, 390)])
@pytest.mark.parametrize("role", ["guest", "user", "admin"])
def test_mobile_menu_keeps_all_role_links_reachable(ui_page, width, height, role):
    page, load = ui_page
    page.set_viewport_size({"width": width, "height": height})
    user = None if role == "guest" else dict(username="test", is_admin=role == "admin")
    load("pages/project/projects_list.html", user=user)
    assert page.locator("#appMain").bounding_box()["y"] < 140
    assert not page.locator("#appNavigation").is_visible()
    toggle = page.get_by_role("button", name="菜单")
    toggle.click()
    assert toggle.get_attribute("aria-expanded") == "true"
    assert page.locator('#appNavigation a[href="/admin"]').count() == (role == "admin")
    assert page.locator(
        '#appNavigation a[href="/auth/login?tab=register"]'
    ).count() == (role == "guest")
    for link in page.locator("#appNavigation a").all():
        box = link.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width
        assert box["height"] >= 44
        assert link.evaluate(
            "e => {const r=e.getBoundingClientRect();const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return !!hit && e.contains(hit)}"
        )
    assert (
        page.locator('#appNavigation a[href="/projects"]').get_attribute("aria-current")
        == "page"
    )
    page.keyboard.press("Escape")
    assert not page.locator("#appNavigation").is_visible()
    assert toggle.evaluate("e => e === document.activeElement")


@pytest.mark.parametrize("width", [390, 320])
def test_profile_tabs_and_long_email_stay_inside_mobile_panel(ui_page, width):
    page, load = ui_page
    page.set_viewport_size({"width": width, "height": 844})
    load("pages/account/profile.html", path="/auth/profile")
    for selector in (".profile-shell", ".profile-tabs", ".profile-info-list"):
        box = page.locator(selector).bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width
    assert page.locator(".profile-info-list").evaluate(
        "e => e.scrollWidth <= e.clientWidth + 1"
    )
    page.get_by_role("tab", name="基本信息").focus()
    page.keyboard.press("ArrowRight")
    assert (
        page.get_by_role("tab", name="我的积分").get_attribute("aria-selected")
        == "true"
    )


@pytest.mark.parametrize(
    "width,height",
    [(1440, 900), (1024, 768), (720, 450), (601, 768), (390, 844), (320, 568)],
)
def test_dashboard_summary_keeps_projects_near_top_without_overflow(
    ui_page, width, height
):
    page, load = ui_page
    page.set_viewport_size({"width": width, "height": height})
    load(
        "pages/project/project_dashboard.html",
        path="/dashboard",
        total_projects=999999,
        completed_projects=999999,
        in_progress_projects=0,
        draft_projects=0,
        recent_projects=[
            dict(
                project_id="ui-test",
                title="长项目名称与研发团队的跨部门协作计划" * 4,
                scenario="general",
                status="completed",
                updated_at=datetime(2026, 10, 2),
            )
        ],
    )
    header = page.locator(".dashboard-header").bounding_box()
    recent = page.locator(".recent-projects-header").bounding_box()
    assert recent["y"] - header["y"] <= (260 if width <= 600 else 140)
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    for card in page.locator(".stat-card").all():
        assert card.evaluate(
            "e => {const card=e.getBoundingClientRect();"
            "return [...e.querySelectorAll('.stat-icon,.stat-number,.stat-title')]"
            ".every(child=>{const r=child.getBoundingClientRect();"
            "return r.left>=card.left && r.right<=card.right && "
            "child.scrollWidth<=child.clientWidth+1})}"
        )
    create = page.get_by_role("link", name="创建项目", exact=True).first
    all_projects = page.get_by_role("link", name="所有项目", exact=True)
    assert create.get_attribute("href") == "/scenarios"
    assert all_projects.get_attribute("href") == "/projects"
    for action in (create, all_projects):
        assert action.bounding_box()["height"] >= (44 if width <= 600 else 40)
        assert action.evaluate(
            "e => {const r=e.getBoundingClientRect();"
            "return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}"
        )


@pytest.mark.parametrize("template", ["projects_list", "project_dashboard"])
def test_project_dialog_traps_focus_and_restores_trigger(ui_page, template):
    page, load = ui_page
    load(f"pages/project/{template}.html")
    trigger = page.get_by_role("button", name="重命名").first
    trigger.click()
    page.locator("#renameProjectTitle").wait_for()
    page.wait_for_timeout(70)
    assert page.locator("#renameModal").get_attribute("aria-modal") == "true"
    for key in ("Tab", "Tab", "Tab", "Shift+Tab", "Shift+Tab"):
        page.keyboard.press(key)
        assert page.evaluate("!!document.activeElement.closest('#renameModal')")
    page.get_by_role("button", name="保存", exact=True).focus()
    page.locator("#renameProjectTitle").fill("")
    page.get_by_role("button", name="保存", exact=True).click()
    assert page.get_by_role("alert").inner_text() == "项目名称不能为空"
    page.keyboard.press("Escape")
    assert not page.locator("#renameModal").is_visible()
    assert trigger.evaluate("e => e === document.activeElement")


def test_config_tabs_support_keyboard_selection(ui_page):
    page, load = ui_page
    load("pages/settings/ai_config.html", path="/ai-config")
    tabs = page.get_by_role("tablist", name="系统配置分类")
    tabs.get_by_role("tab", name="AI 提供者", exact=True).focus()
    page.keyboard.press("ArrowRight")
    selected = tabs.get_by_role("tab", name="生成参数", exact=True)
    assert selected.get_attribute("aria-selected") == "true"
    assert selected.evaluate("e => e === document.activeElement")
    assert page.locator("#generation-params").is_visible()
    page.keyboard.press("Home")
    assert (
        tabs.get_by_role("tab", name="AI 提供者", exact=True).get_attribute(
            "aria-selected"
        )
        == "true"
    )


def test_shared_confirm_can_open_above_project_dialog(ui_page):
    page, load = ui_page
    load("pages/project/projects_list.html")
    page.get_by_role("button", name="重命名").first.click()
    page.wait_for_timeout(70)
    page.evaluate("() => { window.confirmResult = Notify.confirm('示例确认'); }")
    page.locator(".ln-dialog-overlay").wait_for()
    for _ in range(4):
        page.keyboard.press("Tab")
        assert page.evaluate("!!document.activeElement.closest('.ln-dialog-overlay')")
    page.locator(".ln-dialog-overlay").get_by_role("button", name="取消").click()
    assert page.evaluate("!!document.activeElement.closest('#renameModal')")


@pytest.mark.parametrize("width,height", [(390, 844), (844, 390)])
@pytest.mark.parametrize(
    "template,path,trigger,modal",
    [
        (
            "pages/template/global_master_templates.html",
            "/global-master-templates",
            "#createTemplateBtn",
            "#templateModal",
        ),
        (
            "pages/image/image_gallery.html",
            "/image-gallery",
            "#upload-trigger",
            "#upload-modal",
        ),
        (
            "pages/admin/users.html",
            "/admin",
            'button[onclick="showCreateModal()"]',
            "#userModal",
        ),
    ],
)
def test_existing_modal_can_close_and_reopen_with_focus_restored(
    ui_page, width, height, template, path, trigger, modal
):
    page, load = ui_page
    page.set_viewport_size({"width": width, "height": height})
    load(template, path=path)
    button = page.locator(trigger)
    original_overflow = page.evaluate("document.body.style.overflow")
    for _ in range(2):
        button.click()
        dialog = page.locator(modal)
        dialog.wait_for()
        page.wait_for_function(
            "selector => document.querySelector(selector).getAttribute('aria-modal') === 'true'",
            arg=modal,
        )
        for _ in range(8):
            page.keyboard.press("Tab")
            assert page.evaluate(
                "selector => !!document.activeElement.closest(selector)", modal
            )
        content = dialog.locator(".modal-content").bounding_box()
        assert content["y"] >= 0 and content["y"] + content["height"] <= height + 1
        page.keyboard.press("Escape")
        dialog.wait_for(state="hidden")
        page.wait_for_function(
            "selector => document.activeElement.matches(selector)", arg=trigger
        )
        assert page.evaluate("document.body.style.overflow") == original_overflow


def test_long_template_card_fits_mobile_without_changing_preview_document(ui_page):
    page, load = ui_page
    page.set_viewport_size({"width": 320, "height": 568})
    template = dict(
        id=999,
        template_name="Template-" * 12,
        description="长模板描述与布局检查。" * 10,
        tags=["商务", "学术"],
        user_id=1,
        usage_count=3,
        created_by="test",
        html_template='<html><body style="margin:0;width:1280px;height:720px;background:#17263e"><h1>Original preview</h1></body></html>',
    )

    def respond(route):
        assert route.request.method == "GET"
        path = urlsplit(route.request.url).path
        data = (
            dict(
                templates=[template],
                pagination=dict(current_page=1, total_pages=1, total_count=1),
            )
            if path.endswith("/global-master-templates/")
            else template if path.endswith("/999") else {}
        )
        route.fulfill(body=json.dumps(data), content_type="application/json")

    page.route("**/api/global-master-templates/**", respond)
    load("pages/template/global_master_templates.html", path="/global-master-templates")
    card = page.locator(".template-card")
    card.wait_for()
    assert card.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
    assert card.locator("button:visible").count() == 1
    for button in card.locator("button:visible, summary").all():
        box = button.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= 320
    summary = card.locator("summary")
    summary.click()
    assert card.locator("button:visible").count() == 6
    page.keyboard.press("Tab")
    assert card.locator('.ui-action-menu button[data-action="duplicate"]').evaluate(
        "e => e === document.activeElement"
    )
    page.keyboard.press("Escape")
    assert not card.locator("details").evaluate("e => e.open")
    assert summary.evaluate("e => e === document.activeElement")
    summary.click()
    card.locator('button[data-action="edit"]').click()
    assert not card.locator("details").evaluate("e => e.open")
    page.locator("#templateModal").wait_for()
    page.keyboard.press("Escape")
    frame = page.frame_locator(".template-preview-iframe")
    assert (
        frame.get_by_role("heading", name="Original preview").inner_text()
        == "Original preview"
    )
    assert frame.locator("body").evaluate("e => getComputedStyle(e).width") == "1280px"
    assert (
        frame.locator("body").evaluate("e => getComputedStyle(e).backgroundColor")
        == "rgb(23, 38, 62)"
    )


@pytest.mark.parametrize("width,height", [(390, 844), (320, 568), (844, 390)])
def test_admin_menu_keeps_navigation_and_keyboard_exit_reachable(
    ui_page, width, height
):
    page, load = ui_page
    page.set_viewport_size({"width": width, "height": height})
    load("pages/admin/users.html", path="/admin/users")
    nav = page.get_by_role("navigation", name="后台导航")
    assert not nav.is_visible()
    toggle = page.get_by_role("button", name="菜单", exact=True)
    toggle.click()
    for link in nav.locator("a").all():
        box = link.bounding_box()
        assert 0 <= box["x"] <= width - box["width"]
        assert box["height"] >= 44
    page.keyboard.press("Escape")
    assert not nav.is_visible()
    assert toggle.evaluate("e => e === document.activeElement")


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize(
    "template,path,field,button",
    [
        (
            "pages/project/projects_list.html",
            "/projects",
            "#renameProjectTitle",
            "#renameModal button.btn-primary",
        ),
        (
            "pages/settings/ai_config.html",
            "/ai-config",
            'input[name="openai_api_key"]',
            ".provider-header button.btn-primary",
        ),
        (
            "pages/admin/users.html",
            "/admin/users",
            "#userUsername",
            "#userModal button[type=submit]",
        ),
        (
            "pages/auth/login.html",
            "/auth/login",
            "#username",
            "#loginFormElement button[type=submit]",
        ),
    ],
)
def test_primary_controls_share_scale_across_app_shells(
    ui_page, width, template, path, field, button
):
    page, load = ui_page
    page.set_viewport_size({"width": width, "height": 844})
    load(template, path=path)
    if path == "/projects":
        page.get_by_role("button", name="重命名").first.click()
    elif path == "/ai-config":
        page.evaluate("toggleProviderAccordion('openai')")
    elif path == "/admin/users":
        page.locator('button[onclick="showCreateModal()"]').click()
    control = page.locator(field)
    assert control.is_visible()
    assert control.evaluate("e => getComputedStyle(e).fontSize") == (
        "16px" if width < 600 else "14px"
    )
    assert control.bounding_box()["height"] >= (44 if width < 600 else 40)
    visible_control = control.locator("..") if path == "/auth/login" else control
    assert visible_control.bounding_box()["height"] <= (46 if width < 600 else 42)
    primary = page.locator(button).first
    assert primary.evaluate("e => getComputedStyle(e).fontSize") == "14px"
    assert primary.evaluate("e => getComputedStyle(e).borderRadius") == "8px"
    assert (
        primary.evaluate("e => getComputedStyle(e).backgroundColor")
        == "rgb(82, 99, 204)"
    )
    colors = primary.evaluate(
        "e => [getComputedStyle(e).color, getComputedStyle(e).backgroundColor]"
    )

    def luminance(value):
        channels = [int(c) / 255 for c in value[4:-1].split(",")]
        linear = [
            c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
            for c in channels
        ]
        return sum(c * w for c, w in zip(linear, [0.2126, 0.7152, 0.0722]))

    light, dark = sorted([luminance(color) for color in colors], reverse=True)
    assert (light + 0.05) / (dark + 0.05) >= 4.5


@pytest.mark.parametrize("width,height", [(390, 844), (320, 568)])
def test_mobile_auth_has_brand_and_single_input_boundary(ui_page, width, height):
    page, load = ui_page
    page.set_viewport_size({"width": width, "height": height})
    load("pages/auth/login.html", path="/auth/login")
    assert page.locator(".auth-mobile-brand").is_visible()
    assert not page.locator(".brand-panel").is_visible()
    assert (
        page.locator("#username").evaluate("e => getComputedStyle(e).borderTopWidth")
        == "0px"
    )
    panel = page.locator(".form-shell").bounding_box()
    assert panel["x"] >= 0 and panel["x"] + panel["width"] <= width
    toggle = page.locator('#loginFormElement button[data-target="password"]')
    box = toggle.bounding_box()
    assert box["width"] >= 44 and box["height"] >= 44
    assert toggle.evaluate(
        "e => {const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}"
    )
    toggle.click()
    assert page.locator("#password").get_attribute("type") == "text"


@pytest.mark.parametrize("width,height", [(390, 844), (320, 568), (844, 390)])
@pytest.mark.parametrize("auto_repair", [False, True])
def test_ai_sidebar_header_and_long_input_keep_actions_visible(
    ui_page, width, height, auto_repair
):
    page, load = ui_page
    page.set_viewport_size({"width": width, "height": height})
    load(
        "pages/project/project_slides_editor.html",
        path="/projects/ui-test/edit",
        enable_auto_layout_repair=auto_repair,
    )
    page.locator("#aiEditBtn").click()
    page.locator("#aiInputBox").fill("修改当前页的标题并调整正文间距。" * 80)
    page.wait_for_timeout(450)
    for selector in (
        ".ai-edit-sidebar-header h4",
        ".ai-current-slide-info",
        ".ai-header-buttons > button",
        "#aiInputBox",
        "#aiSendBtn",
    ):
        for control in page.locator(selector).all():
            box = control.bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= width + 1
            assert box["y"] >= 0 and box["y"] + box["height"] <= height + 1
    assert page.locator("#aiSendBtn").evaluate(
        "e => {const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}"
    )
    assert page.locator(".ai-header-buttons > button").count() == (
        4 if auto_repair else 3
    )
    if width <= 600:
        assert page.locator(".ai-current-slide-info").bounding_box()["height"] < 30
    for button in page.locator(".ai-header-buttons > button").all():
        assert button.evaluate(
            "e => {const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}"
        )
    page.get_by_role("button", name="关闭 AI 编辑助手").click()
    assert not page.locator("#aiEditSidebar").evaluate(
        "e=>e.classList.contains('open')"
    )


@pytest.mark.parametrize("width", [1440, 390, 320])
@pytest.mark.parametrize(
    "tab",
    ["ai-providers", "generation-params", "jev-config", "app-config", "image-service"],
)
def test_every_settings_tab_keeps_form_fields_inside_page(ui_page, width, tab):
    page, load = ui_page
    page.set_viewport_size({"width": width, "height": 844})
    load("pages/settings/ai_config.html", path="/ai-config")
    page.locator(f'.config-tabs [data-tab="{tab}"]').click()
    panel = page.locator("#" + tab)
    if tab == "ai-providers":
        for provider in ["landppt", "openai", "anthropic", "google"]:
            page.locator(
                f'.provider-header[aria-controls="provider_content_{provider}"]'
            ).locator("span").first.click()
        page.locator("#model-role-toggle-btn").click()
        assert (
            page.locator("#model-role-toggle-btn").get_attribute("aria-expanded")
            == "true"
        )
    elif tab == "image-service":
        page.locator('.toggle-label:has(input[name="enable_image_service"])').click()
        for name in [
            "enable_local_images",
            "enable_network_search",
            "enable_ai_generation",
        ]:
            panel.locator(f'input[name="{name}"]').check()
        for row in panel.locator(".toggle-label").all():
            if row.is_visible():
                assert row.evaluate("e=>getComputedStyle(e).display") == "flex"
                switch = row.locator(".toggle-switch").bounding_box()
                copy = row.locator(":scope > div:last-child").bounding_box()
                assert copy["x"] >= switch["x"] + switch["width"]
    elif tab == "app-config":
        field = panel.locator('input[name="reload"]')
        assert field.bounding_box()["width"] == 18
        assert field.bounding_box()["height"] == 18
        initial = field.is_checked()
        field.focus()
        page.keyboard.press("Space")
        assert field.is_checked() != initial
        page.keyboard.press("Space")
        assert field.is_checked() == initial
    page.wait_for_timeout(100)
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    fields = panel.locator(
        "input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=range]),select,textarea"
    )
    visible = 0
    for field in fields.all():
        if not field.is_visible():
            continue
        visible += 1
        box = field.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width + 1
    assert visible > 0


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844), (320, 568)])
def test_generation_save_stays_clickable_while_editing_last_section(
    ui_page, width, height
):
    page, load = ui_page
    page.set_viewport_size({"width": width, "height": height})
    load("pages/settings/ai_config.html", path="/ai-config")
    page.locator('[data-tab="generation-params"]').click()
    page.locator("#generation-params .config-section").last.scroll_into_view_if_needed()
    assert page.locator("#generation-params .save-bar").bounding_box()["height"] < 100
    save = page.get_by_role("button", name="保存生成参数", exact=True)
    box = save.bounding_box()
    assert box["y"] >= 0 and box["y"] + box["height"] <= height
    assert save.evaluate(
        "e=>{const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}"
    )


def test_generation_slider_and_number_still_synchronize(ui_page):
    page, load = ui_page
    load("pages/settings/ai_config.html", path="/ai-config")
    page.locator('[data-tab="generation-params"]').click()
    number = page.locator('input[name="temperature"]')
    number.fill("1.3")
    assert page.locator('[data-range-for="temperature"]').input_value() == "1.3"
    assert "1.3" in page.locator('[data-value-for="temperature"]').inner_text()
    assert number.get_attribute("aria-labelledby")


@pytest.mark.parametrize("width", [1440, 320])
@pytest.mark.parametrize(
    "provider,name",
    [
        ("openai", "openai_use_responses_api"),
        ("openai", "openai_enable_reasoning"),
        ("anthropic", "anthropic_enable_reasoning"),
    ],
)
def test_provider_switches_share_size_and_keep_keyboard_linkage(
    ui_page, width, provider, name
):
    page, load = ui_page
    page.set_viewport_size({"width": width, "height": 844})
    load("pages/settings/ai_config.html", path="/ai-config")
    page.locator(
        f'.provider-header[aria-controls="provider_content_{provider}"]'
    ).locator("span").first.click()
    field = page.locator(f'input[name="{name}"]')
    assert field.bounding_box()["width"] == 44
    assert field.bounding_box()["height"] == 26
    initial = field.is_checked()
    field.focus()
    page.keyboard.press("Space")
    assert field.is_checked() != initial
    if name.endswith("enable_reasoning"):
        effort = page.locator(f'select[name="{provider}_reasoning_effort"]')
        assert effort.is_disabled() == (not field.is_checked())
    page.keyboard.press("Space")
    assert field.is_checked() == initial


@pytest.mark.parametrize("width", [390, 320])
def test_image_source_keyboard_toggle_and_json_field_remain_usable(ui_page, width):
    page, load = ui_page
    page.set_viewport_size({"width": width, "height": 844})
    load("pages/settings/ai_config.html", path="/ai-config")
    page.locator('[data-tab="image-service"]').click()
    page.locator('.toggle-label:has(input[name="enable_image_service"])').click()
    for name, panel in [
        ("enable_local_images", "local-images-config"),
        ("enable_network_search", "network-search-config"),
        ("enable_ai_generation", "ai-generation-config"),
    ]:
        field = page.locator(f'input[name="{name}"]')
        field.check()
        assert page.locator("#" + panel).is_visible()
        field.focus()
        page.keyboard.press("Space")
        assert not page.locator("#" + panel).is_visible()
        page.keyboard.press("Space")
        assert page.locator("#" + panel).is_visible()
    resolution = page.locator('textarea[name="ai_image_resolution_presets"]')
    resolution.fill(json.dumps({"openai_image": ["1536x1024"] * 80}))
    box = resolution.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= width
    assert page.evaluate("document.documentElement.scrollWidth") <= width


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844), (320, 568)])
def test_admin_settings_tabs_and_long_form_save_are_accessible(ui_page, width, height):
    page, load = ui_page
    page.set_viewport_size({"width": width, "height": height})
    load("pages/admin/community.html", path="/admin/community")
    page.locator(".system-config-block").last.scroll_into_view_if_needed()
    save = page.locator("#saveSystemDefaultsBtn")
    box = save.bounding_box()
    assert box["y"] >= 0 and box["y"] + box["height"] <= height
    assert save.evaluate(
        "e=>{const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}"
    )
    tab = page.locator('.community-tab[data-tab-target="system-defaults"]')
    tab.focus()
    page.keyboard.press("End")
    assert page.locator("#panel-sponsors").is_visible()
    last = page.locator('.community-tab[data-tab-target="sponsors"]')
    assert last.get_attribute("aria-selected") == "true"
    assert last.evaluate("e=>e===document.activeElement")
    page.keyboard.press("Home")
    assert page.locator("#panel-system-defaults").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth") <= width


@pytest.mark.parametrize("width", [1440, 320])
@pytest.mark.parametrize("provider", ["smtp", "resend"])
def test_mail_settings_provider_and_secret_toggle_keep_values(ui_page, width, provider):
    page, load = ui_page
    page.set_viewport_size({"width": width, "height": 844})
    load("pages/admin/smtp.html", path="/admin/smtp")
    page.locator(f'input[name="email_provider"][value="{provider}"]').check()
    assert page.locator(f"#{provider}ConfigSection").is_visible()
    other = "smtp" if provider == "resend" else "resend"
    assert not page.locator(f"#{other}ConfigSection").is_visible()
    field = page.locator(
        "#resend_api_key" if provider == "resend" else "#smtp_password"
    )
    field.fill("fixture-secret")
    button = field.locator("..").locator("button")
    assert button.get_attribute("aria-label")
    button.click()
    assert field.get_attribute("type") == "text"
    assert button.get_attribute("aria-pressed") == "true"
    assert field.input_value() == "fixture-secret"
    button.click()
    assert field.get_attribute("type") == "password"
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert page.get_by_role("button", name="保存邮件设置").count() == 1


@pytest.mark.parametrize("width", [1440, 320])
def test_oauth_switches_keep_keyboard_and_visible_track(ui_page, width):
    page, load = ui_page
    page.set_viewport_size({"width": width, "height": 844})
    load("pages/admin/oauth.html", path="/admin/oauth")
    for provider in ["github", "linuxdo"]:
        field = page.locator(f"#{provider}_enabled")
        was_checked = field.is_checked()
        field.focus()
        page.keyboard.press("Space")
        assert field.is_checked() != was_checked
        track = page.locator(
            f'.oauth-switch[for="{provider}_enabled"] .oauth-switch-track'
        )
        box = track.bounding_box()
        assert box["width"] == 44 and box["height"] == 26
        assert box["x"] >= 0 and box["x"] + box["width"] <= width


@pytest.mark.parametrize(
    "tab,category,field,value,button",
    [
        (
            "generation-params",
            "generation_params",
            "temperature",
            "1.2",
            "保存生成参数",
        ),
        (
            "app-config",
            "app_config",
            "base_url",
            "https://ui.example.invalid",
            "保存应用配置",
        ),
        (
            "image-service",
            "image_service",
            "openai_image_model",
            "fixture-model",
            "保存配置",
        ),
        (
            "ai-providers",
            "model_roles",
            "outline_model_name",
            "fixture-outline",
            "保存模型任务配置",
        ),
    ],
)
def test_settings_layout_keeps_original_save_payload(
    ui_page, tab, category, field, value, button
):
    page, load = ui_page
    requests = []

    def capture(route):
        if route.request.method == "POST":
            requests.append(route.request.post_data_json)
        route.fulfill(
            body='{"success":true,"config":{}}', content_type="application/json"
        )

    page.route("**/api/config/" + category, capture)
    load("pages/settings/ai_config.html", path="/ai-config")
    page.locator(f'[data-tab="{tab}"]').click()
    if category == "model_roles":
        page.locator("#model-role-toggle-btn").click()
    page.locator(f'input[name="{field}"]').fill(value)
    page.get_by_role("button", name=button, exact=True).click()
    page.wait_for_timeout(100)
    assert len(requests) == 1
    assert requests[0]["config"][field] == value


@pytest.mark.parametrize("width,height", [(1440, 900), (320, 568)])
def test_settings_test_result_dialog_fits_and_restores_focus(ui_page, width, height):
    page, load = ui_page
    page.set_viewport_size({"width": width, "height": height})
    load("pages/settings/ai_config.html", path="/ai-config")
    trigger = page.locator('.provider-header button[title="测试连接"]').first
    trigger.focus()
    page.evaluate(
        "document.getElementById('testResults').textContent='隔离测试结果';document.getElementById('testModal').style.display='flex'"
    )
    page.wait_for_timeout(50)
    dialog = page.locator("#testModal")
    assert dialog.get_attribute("aria-modal") == "true"
    close = page.get_by_role("button", name="关闭测试结果")
    box = close.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= width
    assert box["y"] >= 0 and box["y"] + box["height"] <= height
    page.keyboard.press("Escape")
    assert not dialog.is_visible()
    assert trigger.evaluate("e=>e===document.activeElement")


def test_settings_admin_fields_remain_hidden_for_regular_user(ui_page):
    page, load = ui_page
    load(
        "pages/settings/ai_config.html",
        path="/ai-config",
        user=dict(username="reader", is_admin=False),
    )
    assert page.locator('[data-tab="app-config"]').count() == 0
    assert page.locator('input[name="apryse_license_key"]').count() == 0
    assert page.locator('select[name="unattended_default_stop_stage"]').count() == 0


@pytest.mark.parametrize("width", [1440, 320])
@pytest.mark.parametrize("kind", ["success", "error"])
def test_settings_notifications_share_component_and_bound_long_messages(
    ui_page, width, kind
):
    page, load = ui_page
    page.set_viewport_size({"width": width, "height": 844})
    load("pages/settings/ai_config.html", path="/ai-config")
    message = "隔离回归提示：" + "https://" + "service-endpoint-" * 12 + ".invalid"
    page.evaluate("args => showNotification(args[0], args[1])", [message, kind])
    toast = page.locator(".ln-toast--" + kind)
    assert toast.inner_text().startswith(message)
    if kind == "error":
        assert toast.get_attribute("role") == "alert"
    page.wait_for_timeout(250)
    box = toast.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= width
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    toast.get_by_role("button").click()
    assert toast.evaluate("e=>e.classList.contains('ln-out')")


@pytest.fixture
def gallery_page(ui_page):
    page, load = ui_page
    images = [
        dict(
            image_id=f"gallery-{index}",
            title="AI Generated: Abstract network connections 演示素材标题" * 4,
            filename=f"network-{index}.png",
            source_type="ai_generated",
            file_size=35840,
            description="示例素材说明",
            tags="network, abstract",
            category="technology",
            width=400,
            height=300,
            format="png",
            created_at=1790870400,
            access_count=1,
        )
        for index in range(3)
    ]
    requests = []

    def forward(route):
        requests.append((route.request.method, route.request.url))
        assert route.request.method == "GET"
        path = urlsplit(route.request.url).path
        if "/thumbnail/" in path or "/view/" in path:
            route.fulfill(
                body='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300"><rect width="400" height="300" fill="#12354b"/></svg>',
                content_type="image/svg+xml",
            )
        elif "/detail/" in path:
            route.fulfill(
                body=json.dumps({"success": True, "image": images[0]}),
                content_type="application/json",
            )
        else:
            route.fulfill(
                body=json.dumps(
                    {
                        "success": True,
                        "images": images,
                        "pagination": {"current_page": 1, "total_pages": 1},
                    }
                ),
                content_type="application/json",
            )

    page.route("**/api/image/**", forward)
    load("pages/image/image_gallery.html", path="/image-gallery")
    page.locator(".image-item").first.wait_for()
    return page, images, requests


@pytest.mark.parametrize("width", [1440, 1024, 768, 390, 320, 844])
def test_gallery_long_titles_and_card_actions_fit_their_cards(gallery_page, width):
    page, images, _ = gallery_page
    page.set_viewport_size({"width": width, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    for card in page.locator(".image-item").all():
        box = card.bounding_box()
        title = card.locator(".image-title")
        assert title.inner_text() == images[0]["title"]
        assert title.bounding_box()["height"] <= 42
        for button in card.locator(".image-actions button").all():
            bounds = button.bounding_box()
            assert bounds["x"] >= box["x"]
            assert bounds["x"] + bounds["width"] <= box["x"] + box["width"]
            assert bounds["height"] >= (44 if width <= 600 else 36)
        assert card.get_by_role("button", name="复制链接").is_visible()
        assert card.get_by_role("button", name="下载", exact=True).is_visible()
        assert card.get_by_role("button", name="删除图片", exact=True).is_visible()


@pytest.mark.parametrize("width", [1440, 320])
@pytest.mark.parametrize("key", ["Enter", "Space"])
def test_gallery_thumbnail_opens_detail_with_keyboard(gallery_page, width, key):
    page, images, requests = gallery_page
    page.set_viewport_size({"width": width, "height": 844})
    thumbnail = page.locator(".image-thumbnail").first
    thumbnail.focus()
    page.keyboard.press(key)
    page.locator("#image-detail-modal").wait_for(state="visible")
    assert page.locator("#display-title").inner_text() == images[0]["title"]
    assert any(url.endswith("/detail/gallery-0") for _, url in requests)
    page.keyboard.press("Escape")
    assert not page.locator("#image-detail-modal").is_visible()
    assert thumbnail.evaluate("e=>e===document.activeElement")


@pytest.mark.parametrize("width", [1440, 320])
def test_gallery_batch_selection_keeps_state_and_clears_on_exit(gallery_page, width):
    page, _, _ = gallery_page
    page.set_viewport_size({"width": width, "height": 844})
    trigger = page.locator("#batch-mode-btn")
    trigger.click()
    assert trigger.get_attribute("aria-pressed") == "true"
    assert page.locator("#batch-toolbar").is_visible()
    page.locator(".batch-checkbox").first.check()
    assert page.locator("#selected-count").inner_text() == "1"
    assert page.locator(".image-item.selected").count() == 1
    trigger.click()
    assert trigger.get_attribute("aria-pressed") == "false"
    assert page.locator(".batch-checkbox").count() == 0
    assert page.locator(".image-item.selected").count() == 0
    assert page.evaluate("document.documentElement.scrollWidth") <= width


def test_gallery_delete_still_requires_confirmation_and_cancel_does_not_write(
    gallery_page,
):
    page, _, requests = gallery_page
    page.get_by_role("button", name="删除图片", exact=True).first.click()
    dialog = page.locator(".ln-dialog-overlay")
    dialog.wait_for(state="visible")
    dialog.get_by_role("button", name="取消").click()
    assert page.locator(".image-item").count() == 3
    assert all(method == "GET" for method, _ in requests)
