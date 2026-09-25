"""Regressions for fast-mode (template package) pages that repeatedly failed.

Observed in production: writers were given declared ``max_chars`` far above what the
boxes can show, so almost every page needed a slow model repair; repairs then hit a
hard 180 s timeout (recorded as a blank error) or were rejected because the model
wrapped its reply.
"""

import asyncio
import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from landppt.services.slide.package_generation import content_service, workflow
from landppt.services.slide.package_generation.capacity import slot_limits
from landppt.services.slide.package_generation.content_service import (
    ContentService,
    component_contracts,
    parse_json,
)
from landppt.services.slide.package_generation.renderer import (
    PackageRenderError,
    render_page,
)
from landppt.services.slide.package_generation.workflow import describe_error
from landppt.services.slide.svg_page.sanitize import parse_svg, serialize
from landppt.services.template_package.builtin import load_builtin_package
from landppt.services.template_package.schemas import PageContent
from landppt.services.template_package.validator import VALIDATION_IMAGE

PACKAGE = load_builtin_package()
SAMPLE = "只用拉取请求协作，让每次讨论都附着在可审查的具体变更上。评审记录可追溯，AI 降低门槛"


def component(component_id, package=PACKAGE):
    return next(c for c in package.components if c.id == component_id)


def filled_to_budget(item):
    """The component's example with every text field at its full writing budget."""
    data = item.examples[0].model_dump(mode="json")
    for field, limit in slot_limits(item).items():
        text = "".join(itertools.islice(itertools.cycle(SAMPLE), limit))
        if "[" not in field:
            data[field] = text
            continue
        collection, rest = field.split("[", 1)
        index, attribute = int(rest.split("]")[0]), rest.split(".")[1]
        if attribute not in ("value", "unit") and index < len(data[collection]):
            data[collection][index][attribute] = text
    return PageContent.model_validate(data)


def render(package, item, content):
    return render_page(
        package,
        item.id,
        content,
        assets={v.id: VALIDATION_IMAGE for v in content.visual_briefs},
        allowed_image_urls=frozenset({VALIDATION_IMAGE}),
    )


@pytest.mark.parametrize("component_id", [c.id for c in PACKAGE.components])
def test_text_written_to_the_budget_renders_first_time(component_id):
    item = component(component_id)
    render(PACKAGE, item, filled_to_budget(item))


def test_budgets_replace_declared_ceilings_that_cannot_fit():
    points = component("points_3")
    body = next(s for s in points.slots if s.field == "blocks[0].body")
    assert body.max_chars == 240
    limits = slot_limits(points)
    assert limits["blocks[0].body"] < 120
    contract = next(c for c in component_contracts(PACKAGE) if c["id"] == "points_3")
    offered = {s["field"]: s["max_chars"] for s in contract["slots"]}
    assert offered == limits
    too_long = filled_to_budget(points).model_dump(mode="json")
    too_long["blocks"][0]["body"] = SAMPLE * 4
    with pytest.raises(PackageRenderError, match=r"blocks\[0\]\.body.*建议不超过"):
        render(PACKAGE, points, PageContent.model_validate(too_long))


def test_budget_accounts_for_a_box_that_reaches_into_the_slot_below():
    item = component("process_4")
    root = parse_svg(item.svg)
    heading = next(s for s in item.slots if s.field == "blocks[0].heading")
    for node in root.iter():
        if node.get("id") == heading.node_id:
            node.set("data-box-h", "150")  # overlaps the step description
    slots = tuple(
        s.model_copy(update={"max_chars": 200}) if s is heading else s
        for s in item.slots
    )
    taller = item.model_copy(update={"svg": serialize(root), "slots": slots})
    package = PACKAGE.model_copy(update={"components": (taller,)})
    # The box alone would allow five lines; only three are free above the body.
    assert slot_limits(taller)["blocks[0].heading"] <= 3 * 12
    render(package, taller, filled_to_budget(taller))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shape",
    ["envelope", "pages"],
)
async def test_repair_accepts_the_reply_shapes_models_actually_return(shape):
    original = PageContent(slide_id="s1", title="原标题", subtitle="过长的副标题")
    fixed = {"slide_id": "s1", "title": "原标题", "subtitle": "副标题"}
    other = {"slide_id": "s2", "title": "别的页面"}
    replies = {
        "envelope": {"content": fixed, "problems": {}, "instruction": None},
        "pages": {"slide_id": "s1", "pages": [other, fixed]},
    }
    service = ContentService(None)
    service.json_completion = AsyncMock(return_value=(replies[shape], {}))
    result, _ = await service.repair(original, PACKAGE, {"cover": ["overflow"]})
    assert result.subtitle == "副标题"


def test_json_surrounded_by_prose_is_still_parsed():
    assert parse_json('修改如下：\n{"title": "好"}\n以上。') == {"title": "好"}


@pytest.mark.asyncio
async def test_model_timeout_is_reported_with_a_reason(monkeypatch):
    monkeypatch.setattr(content_service, "llm_timeout", AsyncMock(return_value=0.01))
    with pytest.raises(TimeoutError) as error:
        await content_service.bounded_completion(None, asyncio.sleep(1))
    assert "秒" in describe_error(error.value)
    assert describe_error(TimeoutError()) == "TimeoutError"


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_enabled", [True, False])
async def test_failed_repairs_exhaust_budget_then_honor_fallback(
    monkeypatch, fallback_enabled
):
    from landppt.services.slide.package_generation.options import PackageOptions

    content = PageContent(slide_id="s1", title="原始成稿")
    runner = workflow.PackageWorkflow(
        SimpleNamespace(_is_slides_generation_cancelled=AsyncMock(return_value=False)),
        SimpleNamespace(save_content=AsyncMock()),
    )
    runner.content_service.repair = AsyncMock(
        side_effect=[TimeoutError("服务超时"), ValueError("响应结构错误")]
    )
    rendered = object()
    runner._fallback = AsyncMock(return_value=rendered)
    monkeypatch.setattr(
        workflow,
        "render_page",
        lambda *args, **kwargs: (_ for _ in ()).throw(PackageRenderError(["正文溢出"])),
    )
    page = {"content": content.model_dump(), "revision": 1, "assets": {}}
    options = PackageOptions(
        repair_attempts=2, allow_freeform_fallback=fallback_enabled
    )
    call = runner.render_with_repairs(
        "p1", {}, page, PACKAGE, options, "cover", "token"
    )
    if fallback_enabled:
        revision, result = await call
        assert revision == 1 and result is rendered
        assert runner._fallback.await_args.args[1] == content
    else:
        with pytest.raises(PackageRenderError, match="服务超时.*响应结构错误"):
            await call
        runner._fallback.assert_not_awaited()
    assert runner.content_service.repair.await_count == 2
    runner.storage.save_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_during_repair_does_not_start_fallback(monkeypatch):
    from landppt.services.slide.package_generation.options import PackageOptions

    content = PageContent(slide_id="s1", title="原始成稿")
    runner = workflow.PackageWorkflow(
        SimpleNamespace(_is_slides_generation_cancelled=AsyncMock(return_value=False)),
        SimpleNamespace(save_content=AsyncMock()),
    )
    runner.content_service.repair = AsyncMock(side_effect=asyncio.CancelledError)
    runner._fallback = AsyncMock()
    monkeypatch.setattr(workflow, "compatible_components", lambda *a, **kw: [])
    with pytest.raises(asyncio.CancelledError):
        await runner.render_with_repairs(
            "p1",
            {},
            {"content": content.model_dump(), "revision": 1, "assets": {}},
            PACKAGE,
            PackageOptions(),
            None,
            "token",
        )
    runner._fallback.assert_not_awaited()
    runner.storage.save_content.assert_not_awaited()
