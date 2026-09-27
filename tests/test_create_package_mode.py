"""The primary composer must persist the package before outline generation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from landppt.services.template_package.outline_reference import (
    PREFERENCE_KEY,
    REFERENCE_KEY,
)
from landppt.web.route_modules import project_lifecycle_routes as routes


@pytest.fixture
def setup_create(monkeypatch):
    project = SimpleNamespace(project_id="new-project", project_metadata={})
    manager = SimpleNamespace(
        create_project=AsyncMock(return_value=project),
        update_project_status=AsyncMock(),
        update_project_metadata=AsyncMock(return_value=True),
    )
    service = SimpleNamespace(
        project_manager=manager,
        confirm_requirements_and_update_workflow=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(routes, "ppt_service", service)
    monkeypatch.setattr(routes, "get_ppt_service_for_user", lambda uid: service)
    monkeypatch.setattr(
        routes, "maybe_start_unattended_run", AsyncMock(return_value=None)
    )
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_current_user_required] = (
        lambda: SimpleNamespace(id=7, is_admin=False)
    )
    return app, service


@pytest.mark.asyncio
async def test_create_forwards_package_reference_before_outline(
    setup_create, monkeypatch
):
    app, service = setup_create
    preference = {"mode": "package", "version_id": 42, "name": "My package"}
    load = AsyncMock(return_value=(preference, "模板包容量参考"))
    monkeypatch.setattr(routes, "load_package_preference", load)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/projects/create-and-confirm",
            data={
                "topic": "测试",
                "generation_mode": "package",
                "package_version_id": "42",
                "render_mode": "html",
            },
        )
    assert response.status_code == 200, response.text
    load.assert_awaited_once_with(7, 42)
    confirmed = service.confirm_requirements_and_update_workflow.call_args.args[1]
    assert confirmed[PREFERENCE_KEY] == preference
    assert confirmed[REFERENCE_KEY] == "模板包容量参考"
    assert (
        service.project_manager.update_project_metadata.call_args.args[1]["render_mode"]
        == "svg"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["0", "41", "999"])
async def test_invalid_package_is_rejected_before_creating_project(
    setup_create, monkeypatch, version
):
    app, service = setup_create
    monkeypatch.setattr(
        routes,
        "load_package_preference",
        AsyncMock(side_effect=ValueError("请选择已发布模板包")),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/projects/create-and-confirm",
            data={
                "topic": "测试",
                "generation_mode": "package",
                "package_version_id": version,
            },
        )
    assert response.status_code == 422
    service.project_manager.create_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_freeform_keeps_existing_render_mode(setup_create, monkeypatch):
    app, service = setup_create
    load = AsyncMock()
    monkeypatch.setattr(routes, "load_package_preference", load)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/projects/create-and-confirm",
            data={"topic": "测试", "package_version_id": "42"},
        )
    assert response.status_code == 200
    load.assert_not_awaited()
    confirmed = service.confirm_requirements_and_update_workflow.call_args.args[1]
    assert confirmed[PREFERENCE_KEY] == {"mode": "freeform"}
    assert REFERENCE_KEY not in confirmed
    assert (
        service.project_manager.update_project_metadata.call_args.args[1]["render_mode"]
        == "html"
    )
