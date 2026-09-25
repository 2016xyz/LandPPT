import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from landppt.api.template_package_api import GeneratePackage, package_edit_events
from landppt.services.template_package import editor
from landppt.services.template_package.builtin import load_builtin_package


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
