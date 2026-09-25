"""Draft workspace: in-place multi-round edits, page operations, rename, delete, export."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from landppt.api import template_package_api as api
from landppt.database.models import Base, Project, User
from landppt.services.slide.package_generation.options import PackageOptions
from landppt.services.slide.package_generation.storage import PackageStorage
from landppt.services.template_package import editor
from landppt.services.template_package.catalog import (
    PackageCatalog,
    PackageConflict,
    PackageNotFound,
)
from landppt.services.template_package.schemas import TemplatePackage


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'workspace.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        session.add_all(
            [User(id=i, username=f"u{i}", password_hash="x") for i in (1, 2)]
        )
        session.add(
            Project(
                project_id="p1",
                user_id=1,
                title="测试",
                topic="主题",
                scenario="general",
                outline={"title": "测试", "slides": [{"title": "封面"}]},
            )
        )
    yield sessions
    await engine.dispose()


def client_for(catalog):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user_required] = lambda: SimpleNamespace(
        id=catalog.user_id
    )
    app.dependency_overrides[api.catalog] = lambda: catalog
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def ids(pkg):
    return [c["id"] for c in pkg["manifest"]["components"]]


@pytest.mark.asyncio
async def test_draft_is_reused_and_published_version_never_changes(db):
    catalog = PackageCatalog(1, db)
    published = await catalog.install_builtin()
    assert not published["editable"]
    draft = await catalog.editable_draft(published["id"])
    assert draft["editable"] and draft["version"] == 2
    assert (await catalog.editable_draft(draft["id"]))["id"] == draft["id"]
    with pytest.raises(PackageConflict):
        await catalog.mutate_draft(published["id"], lambda c: c[1:])
    assert await catalog.get(published["id"]) == published


@pytest.mark.asyncio
async def test_page_operations_undo_and_stale_hash(db):
    catalog = PackageCatalog(1, db)
    draft = await catalog.editable_draft((await catalog.install_builtin())["id"])
    base = f"/api/global-master-templates/packages/{draft['id']}"
    async with client_for(catalog) as client:
        dup = (
            await client.post(
                f"{base}/components/cover/duplicate",
                params={"expected_hash": draft["content_hash"]},
            )
        ).json()
        assert ids(dup)[:2] == ["cover", "cover-copy"]
        stale = await client.delete(
            f"{base}/components/section",
            params={"expected_hash": draft["content_hash"]},
        )
        assert stale.status_code == 409
        moved = (
            await client.post(
                f"{base}/components/cover-copy/move",
                json={"offset": 2, "expected_hash": dup["content_hash"]},
            )
        ).json()
        assert ids(moved).index("cover-copy") == 3
        removed = (
            await client.delete(
                f"{base}/components/section",
                params={"expected_hash": moved["content_hash"]},
            )
        ).json()
        assert "section" not in ids(removed) and removed["id"] == draft["id"]
        undone = await client.put(
            f"{base}/manifest",
            json={
                "manifest": draft["manifest"],
                "expected_hash": removed["content_hash"],
            },
        )
        assert undone.status_code == 200
        assert ids(undone.json()) == ids(draft)
        missing = await client.delete(f"{base}/components/nope")
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_multi_round_ai_edits_refine_the_same_draft(db, monkeypatch):
    catalog = PackageCatalog(1, db)
    draft = await catalog.editable_draft((await catalog.install_builtin())["id"])
    completion = AsyncMock(
        side_effect=[
            (
                {
                    "operations": [
                        {
                            "action": "modify",
                            "component_id": "cover",
                            "instruction": "改",
                        }
                    ]
                },
                {},
            ),
            ({"changes": {"description": "第一轮封面"}}, {}),
            (
                {
                    "operations": [
                        {
                            "action": "delete",
                            "component_id": "section",
                            "instruction": "删",
                        }
                    ]
                },
                {},
            ),
        ]
    )
    monkeypatch.setattr(editor.ContentService, "json_completion", completion)
    first = [e async for e in editor.edit_package(None, catalog, draft, "改封面")]
    live = [e for e in first if e["type"] == "component"]
    assert live and "<svg" in live[0]["html"]
    round_one = first[-1]["package"]
    assert round_one["id"] == draft["id"] and round_one["version"] == draft["version"]
    second = [
        e
        async for e in editor.edit_package(
            None, catalog, round_one, "删章节", history=["改封面"]
        )
    ]
    final = second[-1]["package"]
    assert final["id"] == draft["id"]
    assert "section" not in ids(final)
    cover = next(c for c in final["manifest"]["components"] if c["id"] == "cover")
    assert cover["description"] == "第一轮封面"
    assert "改封面" in completion.await_args.args[0]


@pytest.mark.asyncio
async def test_single_page_ai_add_gets_unique_id(db, monkeypatch):
    catalog = PackageCatalog(1, db)
    draft = await catalog.editable_draft((await catalog.install_builtin())["id"])
    monkeypatch.setattr(
        editor.ContentService,
        "json_completion",
        AsyncMock(return_value=({"changes": {"description": "新增要点页"}}, {})),
    )
    events = [
        e
        async for e in editor.edit_single_component(
            None, catalog, draft, "add", "新增要点页", reference_id="points_3"
        )
    ]
    saved = events[-1]["package"]
    new_id = events[-1]["changes"][0]["component_id"]
    assert new_id not in ids(draft) and ids(saved)[-1] == new_id
    assert any(e["type"] == "component" and e["html"] for e in events)


@pytest.mark.asyncio
async def test_rename_export_and_delete(db):
    catalog = PackageCatalog(1, db)
    published = await catalog.install_builtin()
    draft = await catalog.editable_draft(published["id"])
    async with client_for(catalog) as client:
        renamed = await client.patch(
            f"/api/global-master-templates/package-templates/{published['template_id']}",
            json={"name": "我的模板"},
        )
        assert renamed.status_code == 200
        assert (await catalog.get(draft["id"]))["manifest"]["name"] == "我的模板"
        pub = await catalog.get(published["id"])
        assert pub["template_name"] == "我的模板"
        assert pub["manifest"] == published["manifest"]

        exported = await client.get(
            f"/api/global-master-templates/packages/{published['id']}/export"
        )
        assert "attachment" in exported.headers["content-disposition"]
        TemplatePackage.model_validate(json.loads(exported.text))
        assert exported.json()["name"] == "我的模板"

        assert (
            await client.delete(
                f"/api/global-master-templates/packages/{published['id']}"
            )
        ).status_code == 409
        assert (
            await client.delete(f"/api/global-master-templates/packages/{draft['id']}")
        ).status_code == 200

    other = PackageCatalog(2, db)
    with pytest.raises(PackageNotFound):
        await other.rename(published["template_id"], "x")
    with pytest.raises(PackageNotFound):
        await other.delete_template(published["template_id"])


@pytest.mark.asyncio
async def test_delete_hides_package_used_by_project_without_breaking_it(db):
    catalog = PackageCatalog(1, db)
    used = await catalog.install_builtin()
    store = PackageStorage(1, db)
    await store.select_package("p1", used["id"], PackageOptions(selector="rules"))
    result = await catalog.delete_template(used["template_id"])
    assert result["hidden"] and result["projects"] == 1
    assert await catalog.list() == []
    snapshot = await store.snapshot("p1")
    assert snapshot["version_id"] == used["id"]
    with pytest.raises(PackageNotFound):
        await catalog.create(
            TemplatePackage.model_validate(used["manifest"]), used["template_id"]
        )
    with pytest.raises(PackageNotFound):
        await store.select_package("p1", used["id"], PackageOptions(selector="rules"))

    unused = await catalog.create(TemplatePackage.model_validate(used["manifest"]))
    assert not (await catalog.delete_template(unused["template_id"]))["hidden"]
    with pytest.raises(PackageNotFound):
        await catalog.get(unused["id"])


@pytest.mark.asyncio
async def test_two_editors_cannot_overwrite_each_other(db):
    catalog = PackageCatalog(1, db)
    source = await catalog.editable_draft((await catalog.install_builtin())["id"])
    manifest = TemplatePackage.model_validate(source["manifest"])
    first = manifest.model_copy(update={"components": manifest.components[1:]})
    second = manifest.model_copy(update={"components": manifest.components[:-1]})
    results = await asyncio.gather(
        catalog.update_draft(source["id"], first, source["content_hash"]),
        catalog.update_draft(source["id"], second, source["content_hash"]),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, PackageConflict) for r in results) == 1


@pytest.mark.asyncio
async def test_delete_rechecks_status_after_concurrent_publish(db, monkeypatch):
    catalog = PackageCatalog(1, db)
    source = await catalog.editable_draft((await catalog.install_builtin())["id"])
    other = PackageCatalog(1, db)
    original_get = catalog.get

    async def stale_get(version_id):
        result = await original_get(version_id)
        await other.transition(version_id, "publish")
        return result

    monkeypatch.setattr(catalog, "get", stale_get)
    with pytest.raises(PackageConflict, match="版本已发布"):
        await catalog.delete_version(source["id"])
    assert (await other.get(source["id"]))["status"] == "published"
