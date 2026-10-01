"""Per-call creation charges use isolated SQLite and the real model boundary."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_pptx_ai_analysis import analysis_page, plan

from landppt.api import template_package_api as api
from landppt.api import template_package_import_api as import_api
from landppt.database.models import Base, CreditTransaction, User
from landppt.services.runtime.runtime_provider_service import RuntimeProviderService
from landppt.services.slide.package_generation import content_service
from landppt.services.slide.package_generation.content_service import ContentService
from landppt.services.template_package import creation_billing, generator
from landppt.services.template_package.builtin import load_builtin_package
from landppt.services.template_package.catalog import PackageCatalog
from landppt.services.template_package.creation_billing import (
    PackageCreationCreditError,
    PackageCreationService,
)
from landppt.web.route_modules import support


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'credits.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        session.add(
            User(id=1, username="owner", password_hash="test", credits_balance=100)
        )
        session.add(
            User(id=2, username="other", password_hash="test", credits_balance=100)
        )
    monkeypatch.setattr(creation_billing.app_config, "enable_credits_system", True)
    monkeypatch.setattr(creation_billing, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(support, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(content_service, "llm_timeout", AsyncMock(return_value=3))
    monkeypatch.setattr(
        generator, "PackageCatalog", lambda uid: PackageCatalog(uid, sessions)
    )
    monkeypatch.setattr(
        import_api, "PackageCatalog", lambda uid: PackageCatalog(uid, sessions)
    )
    yield sessions
    await engine.dispose()


def runtime(replies, provider="landppt"):
    responses = iter(replies)

    async def completion(**kwargs):
        assert "_before_request" not in kwargs
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(
            content=result if isinstance(result, str) else json.dumps(result),
            model="test",
            usage={},
        )

    model = SimpleNamespace(
        collect_streamed_chat_completion=AsyncMock(side_effect=completion)
    )
    service = RuntimeProviderService(SimpleNamespace(user_id=1))
    service._get_role_provider_async = AsyncMock(
        return_value=(model, {"provider": provider, "model": "test"})
    )
    service._get_user_generation_config = AsyncMock(return_value={"temperature": 0.3})
    return service, model.collect_streamed_chat_completion


def app_for(service, monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(import_api.router)
    app.dependency_overrides[api.get_current_user_required] = lambda: SimpleNamespace(
        id=1
    )
    monkeypatch.setattr(support, "get_ppt_service_for_user", lambda uid: service)
    monkeypatch.setattr(import_api, "analysis_service", lambda uid: service)
    return app


async def balance_and_receipts(db, user_id=1):
    async with db() as session:
        balance = (await session.get(User, user_id)).credits_balance
        receipts = (
            await session.scalars(
                select(CreditTransaction).where(CreditTransaction.user_id == user_id)
            )
        ).all()
    return balance, receipts


async def set_balance(db, balance):
    async with db() as session, session.begin():
        await session.execute(
            update(User).where(User.id == 1).values(credits_balance=balance)
        )


def generated_replies(*, retry=False):
    package = load_builtin_package()
    return [
        {"name": "计费测试", "theme": package.theme.model_dump()},
        *(["invalid JSON"] if retry else []),
        *[{"svg": component.svg} for component in package.components],
    ]


def sse_events(response):
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [False, True])
async def test_ai_creation_charges_plan_components_and_actual_retries(
    db, monkeypatch, retry
):
    replies = generated_replies(retry=retry)
    service, calls = runtime(replies)
    app = app_for(service, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/global-master-templates/packages-generate",
            json={"prompt": "科研风格"},
        )
    events = sse_events(response)
    assert events[-1]["type"] == "complete", events
    assert events[-1]["package"]["status"] == "draft"
    count = len(replies)
    balance, receipts = await balance_and_receipts(db)
    assert calls.await_count == count
    assert balance == 100 - 3 * count
    assert len(receipts) == count
    assert all(receipt.amount == -3 for receipt in receipts)
    assert len({receipt.reference_id for receipt in receipts}) == count
    assert (await balance_and_receipts(db, 2))[0] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,enabled", [("openai", True), ("landppt", False)])
async def test_creation_only_bills_landppt_when_credits_are_enabled(
    db, monkeypatch, provider, enabled
):
    monkeypatch.setattr(creation_billing.app_config, "enable_credits_system", enabled)
    service, calls = runtime(generated_replies(), provider)
    app = app_for(service, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/global-master-templates/packages-generate",
            json={"prompt": "科研风格"},
        )
    assert sse_events(response)[-1]["type"] == "complete"
    assert calls.await_count == 11
    assert await balance_and_receipts(db) == (100, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("balance", [2, 3])
async def test_ai_creation_stops_before_the_next_unaffordable_call(
    db, monkeypatch, balance
):
    await set_balance(db, balance)
    service, calls = runtime(generated_replies())
    app = app_for(service, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/global-master-templates/packages-generate",
            json={"prompt": "科研风格"},
        )
    if balance == 2:
        assert response.status_code == 402
        assert calls.await_count == 0
        assert await balance_and_receipts(db) == (2, [])
    else:
        assert response.status_code == 200
        assert sse_events(response)[-1]["status_code"] == 402
        assert calls.await_count == 1
        remaining, receipts = await balance_and_receipts(db)
        assert remaining == 0 and len(receipts) == 1
    assert await PackageCatalog(1, db).list() == []


@pytest.mark.asyncio
async def test_failed_creation_keeps_only_actual_call_charges(db, monkeypatch):
    service, calls = runtime([{"name": "缺少主题"}])
    app = app_for(service, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/global-master-templates/packages-generate",
            json={"prompt": "科研风格"},
        )
    assert sse_events(response)[-1]["type"] == "error"
    assert calls.await_count == 1
    balance, receipts = await balance_and_receipts(db)
    assert balance == 97 and len(receipts) == 1
    assert await PackageCatalog(1, db).list() == []


@pytest.mark.asyncio
async def test_charge_uses_the_provider_resolved_for_each_actual_call(db):
    service, calls = runtime([{"ok": True}, {"ok": True}])
    model, _ = await service.get_role_provider_async("template_generation")
    service._get_role_provider_async.side_effect = [
        (model, {"provider": "openai"}),
        (model, {"provider": " LandPPT "}),
    ]
    content = ContentService(PackageCreationService(service, 1, "AI 模板包创建"))
    await content.json_completion("one", role="template_generation")
    await content.json_completion("two", role="template_generation")
    balance, receipts = await balance_and_receipts(db)
    assert calls.await_count == 2
    assert balance == 97 and len(receipts) == 1


@pytest.mark.asyncio
async def test_concurrent_calls_cannot_overdraw_the_same_balance(db):
    await set_balance(db, 3)
    service, calls = runtime([{"ok": True}])
    content = ContentService(PackageCreationService(service, 1, "AI 模板包创建"))
    results = await asyncio.gather(
        content.json_completion("one", role="template_generation"),
        content.json_completion("two", role="template_generation"),
        return_exceptions=True,
    )
    assert (
        sum(isinstance(result, PackageCreationCreditError) for result in results) == 1
    )
    assert calls.await_count == 1
    balance, receipts = await balance_and_receipts(db)
    assert balance == 0 and len(receipts) == 1


@pytest.mark.asyncio
async def test_receipt_failure_rolls_back_debit_and_blocks_model_request(db):
    service, calls = runtime([{"ok": True}])
    content = ContentService(PackageCreationService(service, 1, "AI 模板包创建"))

    def reject_receipt(*args):
        raise RuntimeError("ledger unavailable")

    event.listen(CreditTransaction, "before_insert", reject_receipt)
    try:
        with pytest.raises(PackageCreationCreditError, match="扣费失败") as failure:
            await content.json_completion("one", role="template_generation")
        assert failure.value.status_code == 503
    finally:
        event.remove(CreditTransaction, "before_insert", reject_receipt)
    calls.assert_not_awaited()
    assert await balance_and_receipts(db) == (100, [])


def fake_analysis(*args, **kwargs):
    pages = [analysis_page(), analysis_page()]
    for index, page in enumerate(pages):
        page["slide"] = index + 1
        page["preview"] = "data:image/png;base64,abc"
    return {"slides": pages}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("vision", [False, True])
async def test_pptx_analysis_bills_every_page_in_both_modes(
    db, monkeypatch, stream, vision
):
    service, calls = runtime([plan(), plan()])
    app = app_for(service, monkeypatch)
    monkeypatch.setattr(import_api, "analyze_pptx", fake_analysis)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/global-master-templates/packages-pptx/analyze",
            params={"stream": str(stream).lower(), "vision": str(vision).lower()},
            files={"file": ("deck.pptx", b"test")},
        )
    assert response.status_code == 200, response.text
    if stream:
        assert json.loads(response.text.splitlines()[-1])["type"] == "complete"
    else:
        assert len(response.json()["slides"]) == 2
    balance, receipts = await balance_and_receipts(db)
    assert calls.await_count == 2
    assert balance == 94 and len(receipts) == 2


@pytest.mark.asyncio
async def test_pptx_analysis_counts_validation_retries(db, monkeypatch):
    service, calls = runtime([{}, plan(), plan()])
    app = app_for(service, monkeypatch)
    monkeypatch.setattr(import_api, "analyze_pptx", fake_analysis)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/global-master-templates/packages-pptx/analyze?stream=true",
            files={"file": ("deck.pptx", b"test")},
        )
    assert json.loads(response.text.splitlines()[-1])["type"] == "complete"
    balance, receipts = await balance_and_receipts(db)
    assert calls.await_count == 3
    assert balance == 91 and len(receipts) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_pptx_insufficient_credits_are_not_retried(db, monkeypatch, stream):
    await set_balance(db, 2)
    service, calls = runtime([])
    app = app_for(service, monkeypatch)
    monkeypatch.setattr(import_api, "analyze_pptx", fake_analysis)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/global-master-templates/packages-pptx/analyze",
            params={"stream": str(stream).lower()},
            files={"file": ("deck.pptx", b"test")},
        )
    if stream:
        terminal = json.loads(response.text.splitlines()[-1])
        assert terminal["type"] == "error" and "积分不足" in terminal["message"]
    else:
        assert response.status_code == 402
    calls.assert_not_awaited()
    assert service._get_role_provider_async.await_count == 1
    assert await balance_and_receipts(db) == (2, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["preserve", "visual"])
@pytest.mark.parametrize("balance", [3, 100])
async def test_pptx_import_bridge_bills_repair_and_visual_calls(
    db, monkeypatch, mode, balance
):
    await set_balance(db, balance)
    service, calls = runtime([{"ok": True}, {"ok": True}])
    app = app_for(service, monkeypatch)

    async def model(component, error, actual_service, *args):
        await ContentService(actual_service).json_completion(
            "repair", role="template_generation"
        )
        return component

    async def visual(component, error, reference, actual_service):
        return await model(component, error, actual_service)

    def importer(data, settings, progress, repair, reconstruct=None):
        component = load_builtin_package().components[0]
        for _ in range(2):
            if reconstruct is not None:
                reconstruct(component, "capacity", "data:image/png;base64,abc")
            else:
                repair(component, "capacity")
        return load_builtin_package(), {"warnings": []}

    monkeypatch.setattr(import_api, "repair_layout", model)
    monkeypatch.setattr(import_api, "reconstruct_layout", visual)
    monkeypatch.setattr(import_api, "import_pptx", importer)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/global-master-templates/packages-pptx/import",
            files={"file": ("deck.pptx", b"test")},
            data={
                "options": json.dumps(
                    {
                        "name": "测试",
                        "mode": mode,
                        "pages": [{"slide": 1, "bindings": {"title": "title"}}],
                    }
                )
            },
        )
    if balance == 3:
        assert response.status_code == 402, response.text
        assert calls.await_count == 1
        assert await PackageCatalog(1, db).list() == []
    else:
        assert response.status_code == 200, response.text
        assert response.json()["package"]["status"] == "draft"
        assert calls.await_count == 2
    remaining, receipts = await balance_and_receipts(db)
    assert remaining == balance - 3 * calls.await_count
    assert len(receipts) == calls.await_count


@pytest.mark.asyncio
async def test_pptx_import_without_model_calls_is_free_even_with_zero_balance(
    db, monkeypatch
):
    await set_balance(db, 0)
    service, calls = runtime([])
    app = app_for(service, monkeypatch)
    monkeypatch.setattr(
        import_api, "import_pptx", lambda *args, **kwargs: (load_builtin_package(), {})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/global-master-templates/packages-pptx/import",
            files={"file": ("deck.pptx", b"test")},
            data={
                "options": json.dumps(
                    {
                        "name": "测试",
                        "pages": [{"slide": 1, "bindings": {"title": "title"}}],
                    }
                )
            },
        )
    assert response.status_code == 200, response.text
    calls.assert_not_awaited()
    assert await balance_and_receipts(db) == (0, [])


@pytest.mark.asyncio
async def test_failed_model_request_is_counted_as_one_actual_call(db):
    service, calls = runtime([RuntimeError("model unavailable")])
    content = ContentService(PackageCreationService(service, 1, "AI 模板包创建"))
    with pytest.raises(RuntimeError, match="model unavailable"):
        await content.json_completion("one", role="template_generation")
    balance, receipts = await balance_and_receipts(db)
    assert calls.await_count == 1
    assert balance == 97 and len(receipts) == 1


@pytest.mark.asyncio
async def test_cancellation_counts_started_call_and_closes_model_request(db):
    service, calls = runtime([])
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def waiting(**kwargs):
        balance, receipts = await balance_and_receipts(db)
        assert balance == 97 and len(receipts) == 1
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    calls.side_effect = waiting
    content = ContentService(PackageCreationService(service, 1, "AI 模板包创建"))
    task = asyncio.create_task(
        content.json_completion("one", role="template_generation")
    )
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and calls.await_count == 1
    balance, receipts = await balance_and_receipts(db)
    assert balance == 97 and len(receipts) == 1
