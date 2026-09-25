import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from landppt.database.models import (
    Base,
    CreditTransaction,
    PackagePageState,
    PackageProjectState,
    Project,
    SlideContentRevision,
    SlideData,
    TemplatePackageVersion,
    User,
)
from landppt.services.slide.package_generation.options import PackageOptions
from landppt.services.slide.package_generation.renderer import render_page
from landppt.services.slide.package_generation.selector import JevClient
from landppt.services.slide.package_generation.storage import PackageStorage
from landppt.services.template_package.builtin import load_builtin_package
from landppt.services.template_package.catalog import (
    PackageCatalog,
    PackageConflict,
    PackageNotFound,
)
from landppt.services.template_package.schemas import PageContent, TemplatePackage


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'packages.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        session.add_all(
            [
                User(id=i, username=f"u{i}", password_hash="test", credits_balance=100)
                for i in (1, 2)
            ]
        )
        session.add_all(
            [
                Project(
                    project_id=f"p{i}",
                    user_id=i,
                    title="测试",
                    topic="主题",
                    scenario="general",
                    outline={
                        "title": "测试",
                        "slides": [{"title": "封面"}, {"title": "总结"}],
                    },
                )
                for i in (1, 2)
            ]
        )
    yield sessions
    await engine.dispose()


async def prepare(db):
    catalog = PackageCatalog(1, db)
    saved = await catalog.install_builtin()
    store = PackageStorage(1, db)
    snapshot = await store.select_package(
        "p1", saved["id"], PackageOptions(selector="rules")
    )
    return catalog, store, saved, snapshot


@pytest.mark.asyncio
async def test_package_versions_publish_retire_and_isolate_users(db):
    catalog, store, saved, snapshot = await prepare(db)
    assert saved["status"] == "published"
    assert await PackageCatalog(2, db).list() == []
    with pytest.raises(PackageNotFound):
        await PackageCatalog(2, db).get(saved["id"])
    with pytest.raises(PackageNotFound):
        await PackageStorage(2, db).snapshot("p1")
    with pytest.raises(PackageNotFound):
        await store.select_package("p2", saved["id"], PackageOptions())
    with pytest.raises(PackageConflict):
        await catalog.transition(saved["id"], "validate")
    newer = await catalog.create(load_builtin_package(), saved["template_id"])
    assert newer["version"] == 2
    assert (await store.snapshot("p1"))["version_id"] == saved["id"]
    await catalog.transition(saved["id"], "retire")
    assert (await store.snapshot("p1"))["manifest"]["version"] == 1
    with pytest.raises(PackageConflict):
        await store.select_package("p1", saved["id"], PackageOptions())


@pytest.mark.asyncio
async def test_content_revisions_and_render_billing_are_atomic_and_idempotent(db):
    _, store, saved, snap = await prepare(db)
    page = snap["pages"][0]
    token = await store.lease("p1")
    content = PageContent(slide_id=page["slide_id"], title="版本一")
    revision, content = await store.save_content(
        "p1", content, page["revision"], "f" * 64, {}, token=token
    )
    rendered = render_page(
        TemplatePackage.model_validate(saved["manifest"]), "cover", content
    )
    revision = await store.commit_render(
        "p1", content.slide_id, revision, rendered, token=token, bill=True
    )
    await store.commit_render(
        "p1", content.slide_id, revision, rendered, token=token, bill=True
    )
    async with db() as session:
        assert (await session.get(User, 1)).credits_balance == 95
        assert len((await session.scalars(select(CreditTransaction))).all()) == 1
        assert len((await session.scalars(select(SlideContentRevision))).all()) == 1
        slide = await session.scalar(select(SlideData))
        assert slide.slide_metadata["content_revision"] == 1
        assert slide.template_id is None
    await store.lease("p1", token, release=True)


@pytest.mark.asyncio
async def test_insufficient_credit_rolls_back_render_and_charge(db):
    _, store, saved, snap = await prepare(db)
    page = snap["pages"][0]
    content = PageContent(slide_id=page["slide_id"], title="内容")
    revision, content = await store.save_content("p1", content, 0, "f" * 64, {})
    async with db() as session, session.begin():
        await session.execute(
            update(User).where(User.id == 1).values(credits_balance=0)
        )
    with pytest.raises(PackageConflict, match="积分"):
        await store.commit_render(
            "p1",
            content.slide_id,
            revision,
            render_page(load_builtin_package(), "cover", content),
            bill=True,
        )
    async with db() as session:
        assert not (await session.scalars(select(CreditTransaction))).all()
        assert not (await session.scalars(select(SlideData))).all()


@pytest.mark.asyncio
async def test_concurrent_edit_and_manual_page_win_over_generator(db):
    _, store, _, snap = await prepare(db)
    page = snap["pages"][0]
    token = await store.lease("p1")
    with pytest.raises(PackageConflict):
        await store.lease("p1")
    content = PageContent(slide_id=page["slide_id"], title="内容")
    revision, content = await store.save_content(
        "p1", content, 0, "f" * 64, {}, token=token
    )
    rendered = render_page(load_builtin_package(), "cover", content)
    revision = await store.commit_render(
        "p1", content.slide_id, revision, rendered, token=token
    )
    await store.mark("p1", content.slide_id, revision, manual=True)
    with pytest.raises(PackageConflict):
        await store.commit_render(
            "p1", content.slide_id, revision, rendered, token=token
        )
    snap = await store.snapshot("p1")
    with pytest.raises(PackageConflict, match="手动"):
        await store.commit_render(
            "p1", content.slide_id, snap["pages"][0]["revision"], rendered, token=token
        )
    await store.commit_render(
        "p1", content.slide_id, snap["pages"][0]["revision"], rendered, restore=True
    )
    async with db() as session:
        state = await session.scalar(
            select(PackagePageState).where(
                PackagePageState.slide_id == content.slide_id
            )
        )
        assert state.history[-1]["html_content"] == rendered.html_content


@pytest.mark.asyncio
async def test_old_generation_lease_cannot_write_after_expiry_or_mode_change(db):
    _, store, _, snap = await prepare(db)
    token = await store.lease("p1")
    async with db() as session, session.begin():
        await session.execute(
            update(PackageProjectState)
            .where(PackageProjectState.project_id == "p1")
            .values(lease_until=0)
        )
    newer = await store.lease("p1")
    assert newer != token
    with pytest.raises(PackageConflict):
        await store.save_content(
            "p1",
            PageContent(slide_id=snap["pages"][0]["slide_id"], title="旧结果"),
            0,
            "x",
            {},
            token=token,
        )
    async with db() as session, session.begin():
        await session.execute(
            update(Project)
            .where(Project.project_id == "p1")
            .values(project_metadata={"generation_mode": "freeform"})
        )
    with pytest.raises(PackageConflict):
        await store.save_content(
            "p1",
            PageContent(slide_id=snap["pages"][0]["slide_id"], title="旧结果"),
            0,
            "x",
            {},
            token=newer,
        )


@pytest.mark.asyncio
async def test_api_owner_scope_preview_and_manual_content_edit(db):
    from fastapi import FastAPI

    from landppt.api import template_package_api as api
    from landppt.auth.middleware import get_current_user_required

    catalog, store, saved, snap = await prepare(db)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.catalog] = lambda: catalog
    app.dependency_overrides[api.storage] = lambda: store
    app.dependency_overrides[get_current_user_required] = lambda: SimpleNamespace(id=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/global-master-templates/packages/{saved['id']}/preview/cover"
        )
        assert response.status_code == 200
        assert "sandbox" in response.headers["content-security-policy"]
        assert (
            await client.get("/api/projects/p2/template-package")
        ).status_code == 404
        page = snap["pages"][0]
        path = f"/api/projects/p1/package-pages/{page['slide_id']}/content"
        payload = {
            "expected_revision": 0,
            "content": {"slide_id": page["slide_id"], "title": "编辑的标题"},
        }
        response = await client.put(path, json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["pages"][0]["content"]["title"] == "编辑的标题"
        assert (await client.put(path, json=payload)).status_code == 409


@pytest.mark.asyncio
async def test_jev_protocol_batches_retries_and_rejects_missing_answers():
    package = load_builtin_package()
    content = package.components[0].examples[0]
    candidates = {content.slide_id: package.components[:2]}
    calls = []

    def handle(request):
        data = json.loads(request.content)
        calls.append(data)
        assert request.url == "https://api.typesafe.ai/v1/systemone"
        assert "state.slides.cover" in data["questions"]["q0"]["instructions"]
        return httpx.Response(
            200,
            json={
                "model": "jev-pinned",
                "answers": {
                    key: {"type": "noul", "noul": 0.8} for key in data["questions"]
                },
                "usage": {"input_tokens": 30},
            },
        )

    scores, usage = await JevClient(
        "fake", transport=httpx.MockTransport(handle)
    ).evaluate([content], candidates)
    assert len(scores) == 2 and len(calls) == 1
    assert usage[0]["model"] == "jev-pinned"
    with pytest.raises(ValueError):
        await JevClient(
            "fake",
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"answers": {}})
            ),
        ).evaluate([content], candidates)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, float("nan"), -1, 1.1, "0.8"])
async def test_jev_invalid_scores_are_not_treated_as_success(value):
    package = load_builtin_package()
    page = package.components[0].examples[0]

    def handle(request):
        # JSON accepts NaN in this fake response; our client must still reject it.
        return httpx.Response(
            200,
            content=json.dumps(
                {"answers": {"q0": {"type": "noul", "noul": value}}}
            ).encode(),
        )

    with pytest.raises(ValueError):
        await JevClient("fake", transport=httpx.MockTransport(handle)).evaluate(
            [page], {page.slide_id: package.components[:1]}
        )


@pytest.mark.asyncio
async def test_workflow_resume_does_not_rewrite_or_rebill_completed_pages(
    db, monkeypatch
):
    from landppt.services.slide.package_generation import workflow

    _, store, _, _ = await prepare(db)
    manager = SimpleNamespace(
        update_stage_status=AsyncMock(), update_project_data=AsyncMock()
    )
    service = SimpleNamespace(
        user_id=1,
        project_manager=manager,
        image_service=None,
        _is_slides_generation_cancelled=AsyncMock(return_value=False),
        get_role_provider_async=AsyncMock(
            return_value=(None, {"provider": "landppt", "model": "test"})
        ),
        _combine_slides_to_full_html=lambda pages, title: "combined",
    )
    monkeypatch.setattr(
        workflow,
        "get_db_config_service",
        lambda: SimpleNamespace(get_all_config=AsyncMock(return_value={})),
    )
    monkeypatch.setattr(workflow.app_config, "enable_credits_system", True)
    runner = workflow.PackageWorkflow(service, store)

    async def expand(snapshot, package, slides, **kwargs):
        return (
            {
                s["slide_id"]: PageContent(
                    slide_id=s["slide_id"], title=s["title"] + "扩写内容"
                )
                for s in slides
            },
            {},
            {"model": "test"},
        )

    runner.content_service.expand = AsyncMock(side_effect=expand)
    first = [e async for e in runner.run("p1")]
    assert '"partial": false' in first[-1], first
    second = [e async for e in runner.run("p1")]
    assert '"partial": false' in second[-1]
    assert runner.content_service.expand.await_count == 1
    async with db() as session:
        assert len((await session.scalars(select(CreditTransaction))).all()) == 2
        assert (await session.get(User, 1)).credits_balance == 90
        assert len((await session.scalars(select(SlideContentRevision))).all()) == 2


@pytest.mark.asyncio
async def test_migration_019_evolves_sqlite_and_is_idempotent(tmp_path):
    from landppt.database.migrations import DatabaseMigration

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    sessions = async_sessionmaker(engine)
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "CREATE TABLE global_master_templates (id INTEGER PRIMARY KEY, template_name TEXT)"
            )
        )
        await session.execute(
            text("INSERT INTO global_master_templates VALUES (1, 'legacy')")
        )
        migration = DatabaseMigration()
        await migration._migration_019_up(session)
        await migration._migration_019_up(session)
        assert (
            await session.execute(
                text("SELECT template_kind FROM global_master_templates")
            )
        ).scalar_one() == "single"
        assert (
            await session.execute(
                text("SELECT count(*) FROM template_package_versions")
            )
        ).scalar_one() == 0
    await engine.dispose()


def test_package_table_definitions_compile_for_postgresql():
    for model in (
        TemplatePackageVersion,
        PackageProjectState,
        PackagePageState,
        SlideContentRevision,
    ):
        ddl = str(CreateTable(model.__table__).compile(dialect=postgresql.dialect()))
        assert "CREATE TABLE" in ddl and "FOREIGN KEY" in ddl


@pytest.mark.asyncio
async def test_failed_content_edit_preserves_previous_revision_and_html(db):
    from fastapi import HTTPException

    from landppt.api.template_package_api import ContentUpdate, update_content

    _, store, _, snap = await prepare(db)
    page = snap["pages"][0]
    sid = page["slide_id"]
    await update_content(
        "p1",
        sid,
        ContentUpdate(
            expected_revision=0, content=PageContent(slide_id=sid, title="原始标题")
        ),
        store,
    )
    before = (await store.snapshot("p1"))["pages"][0]
    with pytest.raises(HTTPException) as error:
        await update_content(
            "p1",
            sid,
            ContentUpdate(
                expected_revision=before["revision"],
                content=PageContent(slide_id=sid, title="长标题" * 160),
            ),
            store,
        )
    assert error.value.status_code == 422
    after = (await store.snapshot("p1"))["pages"][0]
    assert after == before
    async with db() as session:
        assert len((await session.scalars(select(SlideContentRevision))).all()) == 1


@pytest.mark.asyncio
async def test_progress_of_expired_or_replaced_run_cannot_complete_project(db):
    _, store, _, _ = await prepare(db)
    old = await store.lease("p1")
    async with db() as session, session.begin():
        await session.execute(update(PackageProjectState).values(lease_until=0))
    new = await store.lease("p1")
    with pytest.raises(PackageConflict):
        await store.progress("p1", old, "completed", 100, {})
    await store.progress("p1", new, "completed", 100, {})
    async with db() as session:
        assert (
            await session.scalar(select(Project).where(Project.project_id == "p1"))
        ).status == "completed"


@pytest.mark.asyncio
async def test_reconnect_with_zero_balance_reuses_paid_pages(db, monkeypatch):
    from landppt.services.slide.package_generation import workflow

    _, store, _, _ = await prepare(db)
    service = SimpleNamespace(
        user_id=1,
        image_service=None,
        _is_slides_generation_cancelled=AsyncMock(return_value=False),
        get_role_provider_async=AsyncMock(
            return_value=(None, {"provider": "landppt", "model": "test"})
        ),
    )
    monkeypatch.setattr(
        workflow,
        "get_db_config_service",
        lambda: SimpleNamespace(get_all_config=AsyncMock(return_value={})),
    )
    monkeypatch.setattr(workflow.app_config, "enable_credits_system", True)
    runner = workflow.PackageWorkflow(service, store)

    async def expand(snapshot, package, slides, **kwargs):
        return (
            {
                s["slide_id"]: PageContent(slide_id=s["slide_id"], title=s["title"])
                for s in slides
            },
            {},
            {},
        )

    runner.content_service.expand = AsyncMock(side_effect=expand)
    first = [json.loads(e[6:]) async for e in runner.run("p1")]
    assert first[-1]["partial"] is False
    async with db() as session, session.begin():
        await session.execute(
            update(User).where(User.id == 1).values(credits_balance=0)
        )
    again = [json.loads(e[6:]) async for e in runner.run("p1")]
    assert again[-1]["partial"] is False
    assert runner.content_service.expand.await_count == 1


@pytest.mark.asyncio
async def test_cancel_keeps_content_and_resume_only_renders_missing_pages(
    db, monkeypatch
):
    from landppt.services.slide.package_generation import workflow

    _, store, _, _ = await prepare(db)
    service = SimpleNamespace(
        user_id=1,
        image_service=None,
        _is_slides_generation_cancelled=AsyncMock(return_value=False),
        get_role_provider_async=AsyncMock(
            return_value=(None, {"provider": "landppt", "model": "test"})
        ),
    )
    monkeypatch.setattr(
        workflow,
        "get_db_config_service",
        lambda: SimpleNamespace(get_all_config=AsyncMock(return_value={})),
    )
    monkeypatch.setattr(workflow.app_config, "enable_credits_system", True)
    runner = workflow.PackageWorkflow(service, store)

    async def expand(snapshot, package, slides, **kwargs):
        return (
            {
                s["slide_id"]: PageContent(slide_id=s["slide_id"], title=s["title"])
                for s in slides
            },
            {},
            {},
        )

    runner.content_service.expand = AsyncMock(side_effect=expand)
    with pytest.raises(asyncio.CancelledError):
        async for chunk in runner.run("p1"):
            if json.loads(chunk[6:])["type"] == "slide":
                service._is_slides_generation_cancelled.return_value = True
    saved = await store.snapshot("p1")
    assert not saved["running"]
    assert [p["status"] for p in saved["pages"]] == ["completed", "content_ready"]
    service._is_slides_generation_cancelled.return_value = False
    resumed = [json.loads(e[6:]) async for e in runner.run("p1")]
    assert resumed[-1]["partial"] is False
    assert runner.content_service.expand.await_count == 1
    async with db() as session:
        assert len((await session.scalars(select(CreditTransaction))).all()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [[], {"answers": []}, {"answers": {"q0": None}}])
async def test_jev_malformed_structures_raise_validation_error(response):
    package = load_builtin_package()
    page = package.components[0].examples[0]
    client = JevClient(
        "fake",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
    )
    with pytest.raises(ValueError):
        await client.evaluate([page], {page.slide_id: package.components[:1]})


@pytest.mark.asyncio
async def test_content_batch_rejects_duplicates_and_unknown_sources():
    from landppt.services.slide.package_generation.content_service import ContentService

    service = ContentService(None)
    service.json_completion = AsyncMock(
        return_value=(
            {
                "pages": [
                    {"slide_id": "one", "title": "原稿"},
                    {"slide_id": "one", "title": "重复"},
                    {"slide_id": "two", "title": "内容", "source_refs": ["unknown"]},
                    {"slide_id": "three", "title": "有效"},
                ]
            },
            {},
        )
    )
    valid, errors, _ = await service.expand(
        {"outline": {}},
        load_builtin_package(),
        [{"slide_id": sid} for sid in ("one", "two", "three")],
    )
    assert set(valid) == {"three"}
    assert set(errors) == {"one", "two"}


@pytest.mark.asyncio
async def test_ai_template_generation_saves_validated_components_as_draft(
    db, monkeypatch
):
    from landppt.services.template_package import generator

    base = load_builtin_package()
    responses = iter(
        [
            ({"name": "新设计", "theme": base.theme.model_dump()}, {"model": "test"}),
            *[({"svg": c.svg}, {"model": "test"}) for c in base.components],
        ]
    )
    completion = AsyncMock(side_effect=lambda *a, **kw: next(responses))
    monkeypatch.setattr(generator.ContentService, "json_completion", completion)
    monkeypatch.setattr(
        generator, "PackageCatalog", lambda uid: PackageCatalog(uid, db)
    )
    events = [
        e
        async for e in generator.generate_package(
            SimpleNamespace(user_id=1), "现代科研风格"
        )
    ]
    assert events[-1]["type"] == "complete"
    assert not events[-1]["partial"]
    saved = events[-1]["package"]
    assert saved["status"] == "draft"
    assert len(saved["manifest"]["components"]) == 10
    assert (await PackageCatalog(1, db).transition(saved["id"], "publish"))[
        "status"
    ] == "published"


@pytest.mark.asyncio
async def test_automatic_repair_cannot_change_facts():
    from landppt.services.slide.package_generation.content_service import ContentService

    service = ContentService(None)
    original = PageContent(slide_id="s1", title="试点覆盖 30 人")
    service.json_completion = AsyncMock(
        return_value=({"slide_id": "s1", "title": "试点覆盖 50 人"}, {})
    )
    with pytest.raises(ValueError, match="数据"):
        await service.repair(original, load_builtin_package(), "overflow")


@pytest.mark.asyncio
async def test_ai_package_edit_preserves_source_and_pinned_project(db, monkeypatch):
    from landppt.services.template_package import editor

    catalog, store, saved, snapshot = await prepare(db)
    original = TemplatePackage.model_validate(saved["manifest"])
    operations = [
        {"action": "modify", "component_id": "cover", "instruction": "修改封面配色"},
        {"action": "delete", "component_id": "section", "instruction": "删除章节页"},
        {
            "action": "add",
            "component_id": "cover_dark",
            "reference_id": "cover",
            "instruction": "增加深色封面",
        },
    ]
    changed_svg = original.components[0].svg.replace("#F4F1EA", "#EEF2F6")
    completion = AsyncMock(
        side_effect=[
            ({"operations": operations}, {}),
            ({"changes": {"svg": changed_svg, "description": "新的封面配色"}}, {}),
            ({"changes": {"description": "另一个封面"}}, {}),
        ]
    )
    monkeypatch.setattr(editor.ContentService, "json_completion", completion)
    events = [
        e async for e in editor.edit_package(None, catalog, saved, "修改、删除、新增")
    ]
    result = events[-1]
    draft = result["package"]
    assert draft["status"] == "draft" and draft["version"] == 2
    assert draft["template_id"] == saved["template_id"]
    by_id = {c["id"]: c for c in draft["manifest"]["components"]}
    assert "section" not in by_id and "cover_dark" in by_id
    assert by_id["cover"]["description"] == "新的封面配色"
    for component in original.components:
        if component.id not in {"cover", "section"}:
            assert by_id[component.id] == component.model_dump(mode="json")
    assert result["changes"][1]["action"] == "delete"
    assert await catalog.get(saved["id"]) == saved
    assert (await store.snapshot("p1"))["version_id"] == snapshot["version_id"]
    assert (await catalog.transition(draft["id"], "publish"))["status"] == "published"


@pytest.mark.asyncio
async def test_ai_package_edit_route_checks_owner_before_model_and_credits(
    db, monkeypatch
):
    from fastapi import FastAPI

    from landppt.api import template_package_api as api
    from landppt.services.template_package import editor
    from landppt.web.route_modules import support

    catalog, _, saved, _ = await prepare(db)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user_required] = lambda: SimpleNamespace(
        id=1
    )
    app.dependency_overrides[api.catalog] = lambda: catalog
    provider = AsyncMock(return_value=(None, {"provider": "landppt"}))
    monkeypatch.setattr(
        support,
        "get_ppt_service_for_user",
        lambda uid: SimpleNamespace(user_id=uid, get_role_provider_async=provider),
    )
    charge = AsyncMock(return_value=(True, "ok"))
    credit_check = AsyncMock(return_value=(True, 1, 100))
    monkeypatch.setattr(support, "consume_credits_for_operation", charge)
    monkeypatch.setattr(support, "check_credits_for_operation", credit_check)
    completion = AsyncMock(
        return_value=(
            {
                "operations": [
                    {
                        "action": "delete",
                        "component_id": "section",
                        "instruction": "删除章节页",
                    }
                ]
            },
            {},
        )
    )
    monkeypatch.setattr(editor.ContentService, "json_completion", completion)
    url = f"/api/global-master-templates/packages/{saved['id']}/edit"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        app.dependency_overrides[api.catalog] = lambda: PackageCatalog(2, db)
        assert (
            await client.post(url, json={"prompt": "删除章节页"})
        ).status_code == 404
        provider.assert_not_awaited()
        app.dependency_overrides[api.catalog] = lambda: catalog
        credit_check.return_value = (False, 1, 0)
        assert (
            await client.post(url, json={"prompt": "删除章节页"})
        ).status_code == 402
        completion.assert_not_awaited()
        credit_check.return_value = (True, 1, 100)
        response = await client.post(url, json={"prompt": "删除章节页"})
    assert response.status_code == 200
    assert response.headers["x-accel-buffering"] == "no"
    events = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events[-1]["type"] == "complete"
    assert events[-1]["package"]["version"] == 2
    charge.assert_awaited_once()
    # Rounds on one draft share its ID, so the content hash keeps each charge distinct.
    assert charge.await_args.kwargs["reference_id"] == (
        f"package-version:{events[-1]['package']['id']}:"
        f"{events[-1]['package']['content_hash'][:12]}"
    )


@pytest.mark.asyncio
async def test_image_budget_reserves_later_manual_pages(db, monkeypatch):
    from landppt.services.slide.package_generation import workflow

    _, store, saved, _ = await prepare(db)
    snap = await store.select_package(
        "p1",
        saved["id"],
        PackageOptions(selector="rules", allow_images=True, image_budget=1),
    )
    later = snap["pages"][1]
    image_component = next(
        c for c in load_builtin_package().components if c.family == "image"
    )
    content = image_component.examples[0].model_copy(
        update={"slide_id": later["slide_id"]}
    )
    from landppt.services.template_package.validator import VALIDATION_IMAGE

    assets = {content.visual_briefs[0].id: VALIDATION_IMAGE}
    rev, content = await store.save_content(
        "p1", content, later["revision"], "manual", {}
    )
    rendered = render_page(
        load_builtin_package(),
        image_component.id,
        content,
        assets=assets,
        allowed_image_urls=frozenset(assets.values()),
    )
    rev = await store.commit_render(
        "p1", content.slide_id, rev, rendered, assets=assets
    )
    await store.mark("p1", content.slide_id, rev, manual=True)
    service = SimpleNamespace(
        user_id=1,
        image_service=None,
        _is_slides_generation_cancelled=AsyncMock(return_value=False),
        get_role_provider_async=AsyncMock(return_value=(None, {"provider": "test"})),
    )
    monkeypatch.setattr(
        workflow,
        "get_db_config_service",
        lambda: SimpleNamespace(get_all_config=AsyncMock(return_value={})),
    )
    runner = workflow.PackageWorkflow(service, store)

    async def expand(snapshot, package, slides, **kwargs):
        assert kwargs["allow_images"] is False
        return (
            {
                s["slide_id"]: PageContent(slide_id=s["slide_id"], title="无图内容")
                for s in slides
            },
            {},
            {},
        )

    runner.content_service.expand = AsyncMock(side_effect=expand)
    events = [json.loads(e[6:]) async for e in runner.run("p1")]
    assert events[-1]["partial"] is False
    runner.content_service.expand.assert_awaited_once()


@pytest.mark.asyncio
async def test_reordering_keeps_content_ids_and_rendered_pages_together(db):
    _, store, _, snap = await prepare(db)
    for index, page in enumerate(snap["pages"]):
        content = PageContent(slide_id=page["slide_id"], title=f"页面 {index}")
        revision, content = await store.save_content(
            "p1", content, page["revision"], str(index), {}
        )
        await store.commit_render(
            "p1",
            content.slide_id,
            revision,
            render_page(load_builtin_package(), "cover", content),
        )
    async with db() as session, session.begin():
        project = await session.scalar(
            select(Project).where(Project.project_id == "p1")
        )
        project.outline = {
            **project.outline,
            "slides": list(reversed(project.outline["slides"])),
        }
    token = await store.lease("p1")
    reordered = await store.snapshot("p1")
    assert [p["content"]["title"] for p in reordered["pages"]] == ["页面 1", "页面 0"]
    assert [p["slide_index"] for p in reordered["pages"]] == [0, 1]
    async with db() as session:
        project = await session.scalar(
            select(Project).where(Project.project_id == "p1")
        )
        assert [p["title"] for p in project.slides_data] == ["页面 1", "页面 0"]
    await store.lease("p1", token, release=True)


@pytest.mark.asyncio
async def test_changed_outline_rejects_inflight_content(db):
    _, store, _, snap = await prepare(db)
    token = await store.lease("p1")
    async with db() as session, session.begin():
        project = await session.scalar(
            select(Project).where(Project.project_id == "p1")
        )
        project.requirements = "用户刚刚修改的要求"
    page = snap["pages"][0]
    with pytest.raises(PackageConflict, match="资料已修改"):
        await store.save_content(
            "p1",
            PageContent(slide_id=page["slide_id"], title="旧成稿"),
            page["revision"],
            "old",
            {},
            token=token,
        )


@pytest.mark.asyncio
async def test_image_plan_reuses_sources_without_a_second_planning_call(monkeypatch):
    from landppt.services import db_config_service
    from landppt.services.ppt_image_processor import PPTImageProcessor

    config = {
        "enable_image_service": True,
        "enable_local_images": True,
        "enable_network_search": True,
        "enable_ai_generation": True,
    }
    monkeypatch.setattr(
        db_config_service,
        "get_db_config_service",
        lambda: SimpleNamespace(get_config_by_category=AsyncMock(return_value=config)),
    )
    processor = PPTImageProcessor(user_id=1)
    processor._text_completion = AsyncMock(
        side_effect=AssertionError("unexpected planning")
    )
    processor._process_local_images = AsyncMock(return_value=[])
    processor._process_network_images = AsyncMock(
        return_value=[SimpleNamespace(absolute_url="/api/image/view/owned")]
    )
    processor._process_ai_generated_images = AsyncMock()
    content = next(
        c.examples[0] for c in load_builtin_package().components if c.family == "image"
    )
    result = await processor.process_package_images(content, {"topic": "测试"}, 1, 1)
    assert result == {content.visual_briefs[0].id: "/api/image/view/owned"}
    processor._text_completion.assert_not_awaited()
    processor._process_ai_generated_images.assert_not_awaited()
    plan = processor._process_network_images.call_args.args[0]
    assert plan.count == 1 and plan.search_keywords == content.visual_briefs[0].brief


@pytest.mark.asyncio
async def test_jev_matrix_chunks_obey_concurrency_limit():
    package = load_builtin_package()
    pages = [
        package.components[0].examples[0].model_copy(update={"slide_id": f"page{i}"})
        for i in range(10)
    ]
    candidates = {p.slide_id: package.components for p in pages}
    active, peak, calls = 0, 0, 0

    async def handle(request):
        nonlocal active, peak, calls
        active += 1
        peak = max(peak, active)
        calls += 1
        data = json.loads(request.content)
        assert len(data["questions"]) <= 40
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(
            200,
            json={
                "model": "test",
                "answers": {
                    key: {"type": "noul", "noul": 0.8} for key in data["questions"]
                },
            },
        )

    scores, usage = await JevClient(
        "fake", transport=httpx.MockTransport(handle), concurrency=2
    ).evaluate(pages, candidates)
    assert len(scores) == 100 and len(usage) == 3
    assert peak == 2 and calls == 3


@pytest.mark.asyncio
async def test_package_write_recovers_from_real_sqlite_lock(db):
    _, store, _, snap = await prepare(db)
    engine = create_async_engine(
        str(db.kw["bind"].url), connect_args={"timeout": 0.001}
    )
    busy_store = PackageStorage(1, async_sessionmaker(engine, expire_on_commit=False))
    page = snap["pages"][0]
    task = None
    try:
        async with db() as locker, locker.begin():
            await locker.execute(
                update(Project)
                .where(Project.project_id == "p1")
                .values(title=Project.title)
            )
            task = asyncio.create_task(
                busy_store.save_content(
                    "p1",
                    PageContent(slide_id=page["slide_id"], title="锁释放后保存"),
                    page["revision"],
                    "digest",
                    {},
                )
            )
            await asyncio.sleep(0.15)
            assert not task.done()
        revision, _ = await task
        assert revision == page["revision"] + 1
        assert (await store.snapshot("p1"))["pages"][0]["content"][
            "title"
        ] == "锁释放后保存"
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
async def test_expired_package_heartbeat_cannot_revive_run(db):
    _, store, _, _ = await prepare(db)
    token = await store.lease("p1")
    async with db() as session, session.begin():
        await session.execute(
            update(PackageProjectState)
            .where(PackageProjectState.project_id == "p1")
            .values(lease_until=0)
        )
    with pytest.raises(PackageConflict):
        await store.lease("p1", token)
    assert not (await store.snapshot("p1"))["running"]


@pytest.mark.asyncio
async def test_package_snapshot_recovers_from_real_sqlite_read_lock(db):
    _, _, _, _ = await prepare(db)
    engine = create_async_engine(
        str(db.kw["bind"].url), connect_args={"timeout": 0.001}
    )
    store = PackageStorage(1, async_sessionmaker(engine, expire_on_commit=False))
    task = None
    try:
        async with db() as locker:
            await locker.execute(text("BEGIN EXCLUSIVE"))
            task = asyncio.create_task(store.snapshot("p1"))
            await asyncio.sleep(0.15)
            assert not task.done()
            await locker.rollback()
        assert len((await task)["pages"]) == 2
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
async def test_overlong_page_is_repaired_from_field_level_problems(db, monkeypatch):
    from landppt.services.slide.package_generation import workflow

    _, store, _, _ = await prepare(db)
    service = SimpleNamespace(
        user_id=1,
        image_service=None,
        _is_slides_generation_cancelled=AsyncMock(return_value=False),
        get_role_provider_async=AsyncMock(
            return_value=(None, {"provider": "openai", "model": "test"})
        ),
    )
    monkeypatch.setattr(
        workflow,
        "get_db_config_service",
        lambda: SimpleNamespace(get_all_config=AsyncMock(return_value={})),
    )
    runner = workflow.PackageWorkflow(service, store)
    sentence = "只用拉取请求协作，让每次讨论都附着在可审查的具体变更上。"

    def page(slide_id, title, body):
        return PageContent(
            slide_id=slide_id,
            title=title,
            blocks=(
                [{"id": f"b{i}", "heading": "要点", "body": body} for i in range(3)]
                if body
                else ()
            ),
        )

    async def expand(snapshot, package, slides, **kwargs):
        # Within the declared 240 characters, but far beyond what the boxes show.
        return (
            {
                s["slide_id"]: page(
                    s["slide_id"], s["title"], sentence * 7 if i else ""
                )
                for i, s in enumerate(slides)
            },
            {},
            {},
        )

    async def repair(content, package, problems, **kwargs):
        return page(content.slide_id, content.title, sentence), {}

    runner.content_service.expand = AsyncMock(side_effect=expand)
    runner.content_service.repair = AsyncMock(side_effect=repair)
    events = [json.loads(e[6:]) async for e in runner.run("p1")]
    assert events[-1]["partial"] is False, events[-1]
    problems = runner.content_service.repair.await_args.args[2]
    assert set(problems) == {"points_3"}
    assert any(
        "blocks[" in item and "建议不超过" in item for item in problems["points_3"]
    )


@pytest.mark.asyncio
async def test_stop_during_failed_page_retry_keeps_successful_batch_content(
    db, monkeypatch
):
    from landppt.services.slide.package_generation import workflow

    _, store, _, snapshot = await prepare(db)
    first, second = [p["slide_id"] for p in snapshot["pages"]]
    service = SimpleNamespace(
        user_id=1,
        image_service=None,
        _is_slides_generation_cancelled=AsyncMock(return_value=False),
        get_role_provider_async=AsyncMock(return_value=(None, {"provider": "openai"})),
    )
    monkeypatch.setattr(
        workflow,
        "get_db_config_service",
        lambda: SimpleNamespace(get_all_config=AsyncMock(return_value={})),
    )
    runner = workflow.PackageWorkflow(service, store)

    async def interrupted(snapshot, package, slides, **kwargs):
        if len(slides) == 2:
            return (
                {second: PageContent(slide_id=second, title="已写好的第二页")},
                {first: "遗漏"},
                {},
            )
        saved = await store.snapshot("p1")
        assert saved["pages"][1]["content"]["title"] == "已写好的第二页"
        raise asyncio.CancelledError

    runner.content_service.expand = AsyncMock(side_effect=interrupted)
    with pytest.raises(asyncio.CancelledError):
        _ = [e async for e in runner.run("p1")]
    assert not (await store.snapshot("p1"))["running"]

    async def resumed(snapshot, package, slides, **kwargs):
        assert [s["slide_id"] for s in slides] == [first]
        return {first: PageContent(slide_id=first, title="补齐第一页")}, {}, {}

    runner.content_service.expand = AsyncMock(side_effect=resumed)
    events = [json.loads(e[6:]) async for e in runner.run("p1")]
    assert events[-1]["partial"] is False
    runner.content_service.expand.assert_awaited_once()
    assert (await store.snapshot("p1"))["pages"][1]["content_revision"] == 1


@pytest.mark.asyncio
async def test_failed_pages_do_not_count_as_completed_and_resume_only_failures(
    db, monkeypatch
):
    from landppt.services.slide.package_generation import workflow

    _, store, _, snapshot = await prepare(db)
    first, second = [p["slide_id"] for p in snapshot["pages"]]
    service = SimpleNamespace(
        user_id=1,
        image_service=None,
        _is_slides_generation_cancelled=AsyncMock(return_value=False),
        get_role_provider_async=AsyncMock(return_value=(None, {"provider": "openai"})),
    )
    monkeypatch.setattr(
        workflow,
        "get_db_config_service",
        lambda: SimpleNamespace(get_all_config=AsyncMock(return_value={})),
    )
    runner = workflow.PackageWorkflow(service, store)
    runner.content_service.expand = AsyncMock(
        side_effect=[
            (
                {second: PageContent(slide_id=second, title="第二页")},
                {first: "服务超时"},
                {},
            ),
            ({}, {first: "服务超时"}, {}),
        ]
    )
    events = [json.loads(e[6:]) async for e in runner.run("p1")]
    progress = [e for e in events if e["type"] == "progress"]
    assert progress[-1]["completed"] == 1
    assert events[-1]["succeeded"] == 1 and events[-1]["failed_pages"] == [1]
    before = (await store.snapshot("p1"))["pages"][1]
    runner.content_service.expand = AsyncMock(
        return_value=({first: PageContent(slide_id=first, title="补齐第一页")}, {}, {})
    )
    events = [json.loads(e[6:]) async for e in runner.run("p1")]
    assert events[-1]["partial"] is False
    assert [
        s["slide_id"] for s in runner.content_service.expand.await_args.args[2]
    ] == [first]
    after = (await store.snapshot("p1"))["pages"][1]
    assert (after["revision"], after["html_content"]) == (
        before["revision"],
        before["html_content"],
    )
