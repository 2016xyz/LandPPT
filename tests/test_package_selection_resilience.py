import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.exc import OperationalError

from landppt.api.template_package_api import SelectorSettings
from landppt.services.slide.package_generation import follow, selector, workflow
from landppt.services.slide.package_generation.db_retry import retry_transaction
from landppt.services.slide.package_generation.jev_client import JevClient
from landppt.services.slide.package_generation.options import PackageOptions
from landppt.services.template_package.builtin import load_builtin_package


def matrix(count=8):
    base = load_builtin_package().components[0]
    pages = [
        base.examples[0].model_copy(update={"slide_id": f"p{i}"}) for i in range(count)
    ]
    variants = tuple(base.model_copy(update={"id": cid}) for cid in ("a", "b", "bad"))
    return pages, {p.slide_id: variants for p in pages}


def reply(request, value=0.9):
    return httpx.Response(
        200,
        json={
            "model": "test",
            "answers": {
                key: {"type": "noul", "noul": value}
                for key in json.loads(request.content)["questions"]
            },
        },
    )


def test_same_family_variants_vary_without_choosing_bad_fit():
    pages, candidates = matrix(18)
    scores = {
        (p.slide_id, c.id): {"a": 0.94, "b": 0.86, "bad": 0.1}[c.id]
        for p in pages
        for c in candidates[p.slide_id]
    }
    result = list(selector.assign(pages, candidates, scores).values())
    assert set(result) == {"a", "b"}
    assert result.count("b") >= 6
    assert all(len(set(result[i : i + 3])) > 1 for i in range(len(result) - 2))


def test_previous_batch_and_locked_neighbours_are_used():
    pages, candidates = matrix(2)
    scores = {
        (p.slide_id, c.id): 0.9 if c.id != "bad" else 0.1
        for p in pages
        for c in candidates[p.slide_id]
    }
    result = selector.assign(
        pages,
        candidates,
        scores,
        previous_components=["a", "a"],
        fixed_before={"p1": ["a", "a"]},
    )
    assert result == {"p0": "b", "p1": "b"}
    only = {p.slide_id: (candidates[p.slide_id][0],) for p in pages}
    assert set(selector.assign(pages, only, scores).values()) == {"a"}


def test_variation_does_not_override_clear_semantic_advantage_or_budget():
    pages, candidates = matrix(3)
    scores = {
        (p.slide_id, c.id): 1 if c.id == "a" else 0.1
        for p in pages
        for c in candidates[p.slide_id]
    }
    assert set(selector.assign(pages, candidates, scores).values()) == {"a"}
    image_page = load_builtin_package().components[7].examples[0]
    assert image_page.visual_briefs
    with pytest.raises(ValueError):
        selector.assign(
            [image_page],
            {image_page.slide_id: load_builtin_package().components},
            {},
            image_budget=0,
        )


@pytest.mark.asyncio
async def test_small_matrix_supports_more_than_four_parallel_requests():
    pages, candidates = matrix(4)
    active = peak = 0

    async def handle(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return reply(request)

    scores, calls = await JevClient(
        "fake", concurrency=8, transport=httpx.MockTransport(handle)
    ).evaluate(pages, candidates)
    assert len(scores) == 12 and len(calls) == 6
    assert 4 < peak <= 8
    assert SelectorSettings(concurrency=16).concurrency == 16
    with pytest.raises(ValueError):
        SelectorSettings(concurrency=17)


@pytest.mark.asyncio
async def test_partial_failure_retains_successes_and_records_failed_pairs():
    pages, candidates = matrix(2)

    def handle(request):
        data = json.loads(request.content)
        return (
            httpx.Response(503) if "p1" in data["state"]["slides"] else reply(request)
        )

    scores, calls = await JevClient(
        "fake", retries=0, concurrency=2, transport=httpx.MockTransport(handle)
    ).evaluate(pages, candidates)
    assert len(scores) == 3 and {sid for sid, _ in scores} == {"p0"}
    assert calls[1]["error"] == "HTTPStatusError"
    assert len(calls[1]["failed_pairs"]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [429, 504, 529, "timeout", "invalid"])
async def test_transient_responses_retry_only_failed_request(failure, monkeypatch):
    pages, candidates = matrix(1)
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("test")
            if failure == "invalid":
                return httpx.Response(200, content=b"not json")
            return httpx.Response(failure, headers={"Retry-After": "0"})
        return reply(request)

    monkeypatch.setattr(JevClient, "retry_delay", staticmethod(lambda *args: 0))
    scores, _ = await JevClient("fake", transport=httpx.MockTransport(handle)).evaluate(
        pages, candidates
    )
    assert len(scores) == 3 and calls == 2


def test_retry_after_is_respected_and_bounded():
    assert (
        7
        <= JevClient.retry_delay(0, httpx.Response(429, headers={"Retry-After": "7"}))
        <= 60
    )
    assert (
        JevClient.retry_delay(0, httpx.Response(429, headers={"Retry-After": "99999"}))
        == 60
    )


@pytest.mark.asyncio
async def test_auth_failure_is_not_retried():
    pages, candidates = matrix(1)
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        return httpx.Response(401)

    with pytest.raises(httpx.HTTPStatusError):
        await JevClient("fake", transport=httpx.MockTransport(handle)).evaluate(
            pages, candidates
        )
    assert calls == 1


@pytest.mark.asyncio
async def test_cancel_drains_all_inflight_workers():
    pages, candidates = matrix(10)
    ready, blocked = asyncio.Event(), asyncio.Event()
    active = 0

    async def handle(request):
        nonlocal active
        active += 1
        if active == 8:
            ready.set()
        try:
            await blocked.wait()
        finally:
            active -= 1
        return reply(request)

    client = JevClient("fake", concurrency=8, transport=httpx.MockTransport(handle))
    task = asyncio.create_task(client.evaluate(pages, candidates))
    await asyncio.wait_for(ready.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0


@pytest.mark.asyncio
async def test_partial_jev_uses_llm_only_for_missing_pages(monkeypatch):
    package = load_builtin_package()
    pages = [
        package.components[0].examples[0].model_copy(update={"slide_id": f"p{i}"})
        for i in range(2)
    ]
    candidates = {p.slide_id: selector.compatible_components(package, p) for p in pages}
    partial = {(pages[0].slide_id, c.id): 0.9 for c in candidates[pages[0].slide_id]}
    monkeypatch.setattr(
        JevClient, "evaluate", AsyncMock(return_value=(partial, [{"model": "jev"}]))
    )
    content = SimpleNamespace(
        json_completion=AsyncMock(
            return_value=({"choices": {"p1": "cover"}}, {"model": "llm"})
        )
    )
    result, audit = await selector.select_components(
        package,
        pages,
        PackageOptions(),
        {"jev_enabled": True, "jev_api_key": "fake"},
        content,
    )
    payload = json.loads(content.json_completion.call_args.args[0].split("\n", 1)[1])
    assert [p["slide_id"] for p in payload["pages"]] == ["p1"]
    assert set(result) == {"p0", "p1"} and audit["selector"] == "jev+llm"
    assert audit["calls"][0]["model"] == "jev"


@pytest.mark.asyncio
async def test_database_retries_only_rollback_conflicts():
    calls = 0

    @retry_transaction
    async def operation():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError(
                "", {}, sqlite3.OperationalError("database is locked")
            )
        return "saved"

    assert await operation() == "saved" and calls == 2
    fatal = AsyncMock(
        side_effect=OperationalError(
            "", {}, RuntimeError("connection lost after commit")
        )
    )
    with pytest.raises(OperationalError):
        await retry_transaction(fatal)()
    assert fatal.await_count == 1


@pytest.mark.asyncio
async def test_heartbeat_recovers_from_temporary_database_failure(monkeypatch):
    failure = OperationalError("", {}, sqlite3.OperationalError("database is locked"))
    storage = SimpleNamespace(
        lease=AsyncMock(side_effect=[failure, None, asyncio.CancelledError()])
    )
    runner = workflow.PackageWorkflow(SimpleNamespace(user_id=1), storage)
    monkeypatch.setattr(workflow.asyncio, "sleep", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await runner._heartbeat("p1", "token")
    assert storage.lease.await_count == 3


@pytest.mark.asyncio
async def test_follower_recovers_from_transient_read_failure(monkeypatch):
    failed_read = OperationalError(
        "", {}, sqlite3.OperationalError("database is locked")
    )
    storage = SimpleNamespace(
        snapshot=AsyncMock(
            side_effect=[
                failed_read,
                {"running": True, "pages": []},
                {"running": False, "pages": []},
            ]
        )
    )
    monkeypatch.setattr(follow, "PackageStorage", lambda _: storage)
    monkeypatch.setattr(follow.asyncio, "sleep", AsyncMock())
    service = SimpleNamespace(
        user_id=1,
        project_manager=SimpleNamespace(
            get_stage_status=AsyncMock(
                side_effect=[{"status": "running"}, {"status": "completed"}]
            )
        ),
    )
    events = [chunk async for chunk in follow.follow_package_generation(service, "p1")]
    assert '"type": "complete"' in events[-1]
    assert not any('"type": "error"' in chunk for chunk in events)


@pytest.mark.asyncio
async def test_follower_counts_current_ready_pages_even_without_phase_change(
    monkeypatch,
):
    def page(index, ready):
        return {
            "slide_id": f"s{index}",
            "slide_index": index,
            "revision": 1,
            "html_content": "<svg/>" if ready else None,
            "status": "completed" if ready else "failed",
            "manual": False,
            "locked": False,
            "content": {"title": "页面"},
            "render_metadata": {},
        }

    storage = SimpleNamespace(
        snapshot=AsyncMock(
            side_effect=[
                {"running": True, "pages": [page(0, True), page(1, False)]},
                {"running": True, "pages": [page(0, True), page(1, True)]},
                {"running": True, "pages": [page(0, False), page(1, True)]},
                {"running": False, "pages": [page(0, False), page(1, True)]},
            ]
        )
    )
    monkeypatch.setattr(follow, "PackageStorage", lambda _: storage)
    monkeypatch.setattr(follow.asyncio, "sleep", AsyncMock())
    stage = {
        "status": "running",
        "result": {"package_phase": "render", "message": "正在排版"},
    }
    service = SimpleNamespace(
        user_id=1,
        project_manager=SimpleNamespace(
            get_stage_status=AsyncMock(
                side_effect=[stage, stage, stage, {"status": "failed"}]
            )
        ),
    )
    events = [
        json.loads(e[6:])
        async for e in follow.follow_package_generation(service, "p1")
        if e.startswith("data:")
    ]
    progress = [e for e in events if e["type"] == "progress"]
    assert [e["completed"] for e in progress[:3]] == [1, 2, 1]
    assert all("current" not in e for e in progress)
    assert events[-1]["type"] == "error"


@pytest.mark.asyncio
async def test_finished_package_with_missing_page_reports_partial_completion(
    monkeypatch,
):
    pages = [
        {
            "slide_id": f"s{i}",
            "slide_index": i - 1,
            "revision": 1,
            "html_content": "<svg/>" if i != 5 else None,
            "status": "completed" if i != 5 else "failed",
            "manual": False,
            "locked": False,
            "content": {},
            "render_metadata": {},
        }
        for i in range(1, 10)
    ]
    storage = SimpleNamespace(
        snapshot=AsyncMock(
            side_effect=[
                {"running": True, "pages": pages},
                {"running": False, "pages": pages},
            ]
        )
    )
    monkeypatch.setattr(follow, "PackageStorage", lambda _: storage)
    monkeypatch.setattr(follow.asyncio, "sleep", AsyncMock())
    service = SimpleNamespace(
        user_id=1,
        project_manager=SimpleNamespace(
            get_stage_status=AsyncMock(
                side_effect=[
                    {"status": "running"},
                    {
                        "status": "failed",
                        "result": {
                            "failed_pages": [5],
                            "message": "第 5 页未能生成，其余页面已保存",
                        },
                    },
                ]
            )
        ),
    )
    events = [
        json.loads(e[6:])
        async for e in follow.follow_package_generation(service, "p1")
        if e.startswith("data:")
    ]
    assert events[-1] == {
        "type": "complete",
        "total": 9,
        "succeeded": 8,
        "partial": True,
        "failed_pages": [5],
        "message": "第 5 页未能生成，其余页面已保存",
    }
