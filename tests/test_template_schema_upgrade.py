"""Exercise the reported endpoints against an actual pre-package database."""

import os
import uuid
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from landppt.database.models import Base, GlobalMasterTemplate, User


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def legacy_database(request, tmp_path, monkeypatch):
    from landppt.database import database, migrations

    admin_engine = None
    schema = None
    if request.param == "postgresql":
        url = os.environ.get("LANDPPT_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("Set LANDPPT_TEST_POSTGRES_URL for PostgreSQL upgrade coverage")
        admin_engine = create_async_engine(url)
        schema = "landppt_upgrade_" + uuid.uuid4().hex
        async with admin_engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
        engine = create_async_engine(
            url, connect_args={"server_settings": {"search_path": schema}}
        )
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session, session.begin():
            session.add(User(id=1, username="legacy-user", password_hash="test"))
            session.add(
                GlobalMasterTemplate(
                    id=1,
                    template_name="Legacy template",
                    html_template="<div>Legacy</div>",
                    is_default=True,
                    is_active=True,
                    tags=None,
                )
            )
        async with engine.begin() as connection:
            for table in (
                "slide_content_revisions",
                "package_page_states",
                "package_project_states",
                "template_package_versions",
            ):
                await connection.execute(text(f"DROP TABLE {table}"))
            await connection.execute(
                text("ALTER TABLE global_master_templates DROP COLUMN template_kind")
            )
        monkeypatch.setattr(database, "async_engine", engine)
        monkeypatch.setattr(migrations, "AsyncSessionLocal", sessions)
        manager = migrations.DatabaseMigration()
        async with sessions() as session:
            await manager._create_migration_table(session)
            for migration in manager.migrations:
                if migration["version"] != "019":
                    await manager._record_migration(session, migration)
        yield sessions, manager
    finally:
        await engine.dispose()
        if admin_engine:
            async with admin_engine.begin() as connection:
                await connection.execute(DropSchema(schema, cascade=True))
            await admin_engine.dispose()


@pytest.mark.asyncio
async def test_migration_restores_all_three_template_endpoints(
    legacy_database, monkeypatch
):
    from landppt import cli
    from landppt.api import global_master_template_api, template_package_api
    from landppt.auth import middleware
    from landppt.services.template import global_master_template_service
    from landppt.services.template_package import catalog

    sessions, manager = legacy_database
    monkeypatch.setattr(cli, "migration_manager", manager)
    monkeypatch.setattr(global_master_template_service, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(catalog, "AsyncSessionLocal", sessions)
    user = SimpleNamespace(id=1, is_admin=False)
    auth = middleware.AuthMiddleware()

    async def cached_user(session_id):
        return user

    monkeypatch.setattr(auth, "_get_user_from_session_cache", cached_user)
    app = FastAPI()
    app.middleware("http")(auth)
    app.include_router(template_package_api.router)
    app.include_router(global_master_template_api.router)
    app.dependency_overrides[middleware.get_db] = lambda: None
    paths = (
        "/api/global-master-templates/?active_only=true&page=1&page_size=6",
        "/api/global-master-templates/packages",
        "/api/global-master-templates/default/template",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        cookies={"session_id": "valid-session"},
    ) as client:
        for path in paths:
            assert (await client.get(path)).status_code == 500
        assert (await client.get(paths[0])).json()[
            "detail"
        ] == "Failed to get templates"
        assert (await client.get(paths[2])).json()[
            "detail"
        ] == "Failed to get default template"
        assert await cli._migrate() is True
        assert await cli._migrate() is True
        responses = [await client.get(path) for path in paths]
        assert [response.status_code for response in responses] == [200, 200, 200]
        assert responses[0].json()["templates"][0]["template_name"] == "Legacy template"
        for payload in (responses[0].json()["templates"][0], responses[2].json()):
            assert payload["description"] == payload["created_by"] == ""
            assert payload["tags"] == []
        assert responses[1].json() == {"packages": []}
        assert responses[2].json()["html_template"] == "<div>Legacy</div>"
        async with sessions() as session:
            assert (
                await session.execute(
                    text(
                        "SELECT template_kind FROM global_master_templates WHERE id = 1"
                    )
                )
            ).scalar_one() == "single"
        assert (await manager.get_migration_status())["pending_migrations"] == []
