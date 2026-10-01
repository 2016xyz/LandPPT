import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from landppt.ai.base import AIResponse
from landppt.api.template_package_api import GeneratePackage, package_edit_events
from landppt.services.slide.package_generation import content_service
from landppt.services.template_package import editor
from landppt.services.template_package.builtin import load_builtin_package
from landppt.services.template_package.reply_stream import ReplyPrefix


def operation(action, component_id="cover", **kwargs):
    return dict(
        action=action, component_id=component_id, instruction="编辑要求", **kwargs
    )


def source():
    return dict(
        id=1,
        template_id=1,
        user_id=1,
        manifest=load_builtin_package().model_dump(mode="json"),
    )


@pytest.mark.parametrize(
    "operations",
    [
        [operation("delete", "missing")],
        [operation("add", "cover", reference_id="cover")],
        [operation("add", "new", reference_id="missing")],
        [operation("modify"), operation("delete")],
        [operation("add", "new")],
        [operation("delete", c.id) for c in load_builtin_package().components],
    ],
)
def test_edit_rejects_invalid_targets(operations):
    package = load_builtin_package()
    with pytest.raises(ValueError):
        editor.EditPlan(operations=operations).check_targets(
            package, {c.id: c for c in package.components}
        )


@pytest.mark.asyncio
async def test_component_failure_does_not_save_partial_deletion(monkeypatch):
    catalog = SimpleNamespace(create=AsyncMock())
    invalid = '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    completion = AsyncMock(
        side_effect=[
            ({"operations": [operation("delete", "section"), operation("modify")]}, {}),
            ({"changes": {"svg": invalid}}, {}),
            ({"changes": {"svg": invalid}}, {}),
        ]
    )
    monkeypatch.setattr(editor.ContentService, "json_completion", completion)
    original = source()
    with pytest.raises(ValueError, match="未保存任何修改"):
        _ = [
            e
            async for e in editor.edit_package(
                None, catalog, original, "修改封面，删除章节页"
            )
        ]
    catalog.create.assert_not_awaited()
    assert original == source()
    assert completion.await_count == 3
    assert "previous_error" in completion.await_args.args[0]


@pytest.mark.asyncio
async def test_noop_is_not_saved(monkeypatch):
    catalog = SimpleNamespace(create=AsyncMock())
    monkeypatch.setattr(
        editor.ContentService,
        "json_completion",
        AsyncMock(
            side_effect=[
                ({"operations": [operation("modify")]}, {}),
                (
                    {
                        "changes": {
                            "description": load_builtin_package()
                            .components[0]
                            .description
                        }
                    },
                    {},
                ),
            ]
        ),
    )
    with pytest.raises(ValueError, match="未产生实际修改"):
        _ = [e async for e in editor.edit_package(None, catalog, source(), "修改")]
    catalog.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_template_edit_is_copied(monkeypatch):
    catalog = SimpleNamespace(create=AsyncMock(return_value={"id": 2}))
    monkeypatch.setattr(
        editor.ContentService,
        "json_completion",
        AsyncMock(return_value=({"operations": [operation("delete", "section")]}, {})),
    )
    original = {**source(), "user_id": None}
    events = [
        e async for e in editor.edit_package(None, catalog, original, "删除章节页")
    ]
    assert events[-1]["type"] == "complete"
    assert catalog.create.await_args.args[1] is None


@pytest.mark.asyncio
async def test_invalid_plan_json_is_retried_with_feedback(monkeypatch):
    catalog = SimpleNamespace(create=AsyncMock(return_value={"id": 2}))
    completion = AsyncMock(
        side_effect=[
            ValueError("invalid JSON"),
            ({"operations": [operation("delete", "section")]}, {}),
        ]
    )
    monkeypatch.setattr(editor.ContentService, "json_completion", completion)
    events = [
        e async for e in editor.edit_package(None, catalog, source(), "删除章节页")
    ]
    assert events[-1]["type"] == "complete"
    assert "invalid JSON" in completion.await_args.args[0]
    catalog.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_edit_stream_heartbeat_does_not_cancel_model_and_bills_once():
    released = asyncio.Event()
    closed = asyncio.Event()

    async def events():
        try:
            await released.wait()
            yield {"type": "complete", "package": {"id": 3}}
        finally:
            closed.set()

    bill = AsyncMock()
    stream = package_edit_events(events(), bill, heartbeat_seconds=0.001)
    assert await anext(stream) == ": heartbeat\n\n"
    assert not closed.is_set()
    released.set()
    item = json.loads((await anext(stream))[6:])
    assert item["type"] == "complete"
    assert [e async for e in stream] == []
    assert closed.is_set()
    bill.assert_awaited_once_with(item)


@pytest.mark.asyncio
async def test_disconnected_edit_stream_cancels_pending_model():
    closed = asyncio.Event()

    async def events():
        try:
            await asyncio.Event().wait()
            yield {}
        finally:
            closed.set()

    bill = AsyncMock()
    stream = package_edit_events(events(), bill, heartbeat_seconds=0.001)
    await anext(stream)
    await stream.aclose()
    assert closed.is_set()
    bill.assert_not_awaited()


@pytest.mark.asyncio
async def test_upstream_failure_is_terminal_error_without_leaking_secrets():
    async def events():
        raise RuntimeError("secret-upstream-url")
        yield {}

    bill = AsyncMock()
    result = [e async for e in package_edit_events(events(), bill)]
    assert json.loads(result[-1][6:])["type"] == "error"
    assert "secret-upstream-url" not in result[-1]
    bill.assert_not_awaited()


def test_blank_edit_instructions_rejected():
    with pytest.raises(ValueError):
        GeneratePackage(prompt="  ")


@pytest.mark.parametrize("fence", ["", "```json\n"])
def test_reply_parser_handles_split_escapes_without_displaying_page_code(fence):
    reply = '我会调整“图文页”，保留标题。\n图片写入 "这里" 🚀'
    raw = fence + json.dumps(
        {"reply": reply, "changes": {"svg": "<svg>隐藏的页面代码</svg>"}},
        ensure_ascii=True,
    )
    parser = ReplyPrefix()
    text = "".join(parser.feed(char) for char in raw)
    assert text == reply
    assert parser.finished and not parser.buffer


def test_reply_parser_does_not_show_nested_fields_or_unfiltered_reasoning():
    for raw in (
        '{"changes":{"reply":"页面内容"},"reply":"最终说明"}',
        '<think>{"reply":"内部推理"}</think>{"reply":"正常回复"}',
    ):
        parser = ReplyPrefix()
        assert "".join(parser.feed(char) for char in raw) == ""


def test_reply_length_is_bounded_before_the_model_finishes():
    with pytest.raises(ValueError, match="4000"):
        ReplyPrefix().feed('{"reply":"' + "字" * 4001)


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", [False, True])
async def test_edit_reply_is_delivered_before_model_finishes_and_cancel_is_atomic(
    monkeypatch, disconnect
):
    monkeypatch.setattr(content_service, "llm_timeout", AsyncMock(return_value=10))
    release, closed = asyncio.Event(), asyncio.Event()
    data = {
        "reply": "我会删除章节页，保留其他页面。",
        "operations": [operation("delete", "section")],
    }
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    first = '{"reply":"我会删除'

    async def complete(role, *, on_chunk, **kwargs):
        assert role == "template_generation" and kwargs["stream_response"]
        try:
            await on_chunk(first)
            await release.wait()
            await on_chunk(raw[len(first) :])
            return AIResponse(content=raw, model="test", usage={})
        finally:
            closed.set()

    catalog = SimpleNamespace(create=AsyncMock(return_value={"id": 2}))
    service = SimpleNamespace(_chat_completion_for_role=complete)
    events = editor.edit_package(service, catalog, source(), "删除章节页")
    assert (await anext(events))["type"] == "progress"
    assert (await anext(events))["reset"] is True
    assert (await anext(events))["text"] == "我会删除"
    catalog.create.assert_not_awaited()
    if disconnect:
        await events.aclose()
        assert closed.is_set()
        catalog.create.assert_not_awaited()
    else:
        release.set()
        remaining = [event async for event in events]
        assert remaining[0]["text"] == "章节页，保留其他页面。"
        assert remaining[-1]["type"] == "complete" and closed.is_set()
        catalog.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_replaces_the_invalid_plans_streamed_reply(monkeypatch):
    replies = [
        {
            "reply": "先删除不存在的页面。",
            "operations": [operation("delete", "missing")],
        },
        {"reply": "改为删除章节页。", "operations": [operation("delete", "section")]},
    ]

    async def complete(self, prompt, *, on_chunk, **kwargs):
        data = replies.pop(0)
        await on_chunk(json.dumps(data, ensure_ascii=False))
        return data, {}

    monkeypatch.setattr(editor.ContentService, "json_completion", complete)
    catalog = SimpleNamespace(create=AsyncMock(return_value={"id": 2}))
    events = [
        e async for e in editor.edit_package(None, catalog, source(), "删除章节页")
    ]
    text = ""
    resets = 0
    for event in events:
        if event["type"] == "reply":
            if event.get("reset"):
                text = ""
                resets += 1
            text += event["text"]
    assert resets == 2
    assert text == "改为删除章节页。"
    assert events[-1]["type"] == "complete"
    catalog.create.assert_awaited_once()
