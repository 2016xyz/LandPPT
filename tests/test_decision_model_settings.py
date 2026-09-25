import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from landppt.api import template_package_api as api
from landppt.services import db_config_service
from landppt.services.slide.package_generation import selector
from landppt.services.slide.package_generation.jev_client import (
    DEFAULT_DECISION_ENDPOINT,
    JevClient,
)
from landppt.services.slide.package_generation.options import PackageOptions
from landppt.services.template_package.builtin import load_builtin_package


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/test",
        "https://user:pass@example.com/api",
        "https://example.com/#secret",
        "https://",
        "https://example.com:bad/api",
    ],
)
def test_invalid_decision_endpoints_are_rejected(url):
    with pytest.raises(ValueError):
        api.SelectorSettings(endpoint_url=url)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["jev", "openai"])
async def test_custom_endpoint_model_key_and_real_score_validation(protocol):
    seen = []

    def handle(request):
        data = json.loads(request.content)
        assert str(request.url) == "https://decision.test/custom/score"
        assert request.headers["authorization"] == "Bearer test-key"
        assert data["model"] == "custom-model"
        seen.append(data)
        if protocol == "jev":
            return httpx.Response(
                200, json={"answers": {"q0": {"type": "noul", "noul": 0.8}}}
            )
        assert data["messages"][0]["role"] == "system"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"scores":{"q0":0.8}}'},
                    }
                ],
                "model": "custom-model",
            },
        )

    c = load_builtin_package().components[0]
    page = c.examples[0]
    scores, _ = await JevClient(
        "test-key",
        "custom-model",
        retries=0,
        endpoint_url="https://decision.test/custom/score",
        protocol=protocol,
        transport=httpx.MockTransport(handle),
    ).evaluate([page], {page.slide_id: [c]})
    assert scores == {(page.slide_id, c.id): 0.8} and len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    ['{"scores":{"q0":true}}', '{"scores":{"q0":4}}', '{"scores":{}}', "not json"],
)
async def test_openai_decision_response_must_contain_all_valid_scores(content):
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )
    )
    c = load_builtin_package().components[0]
    page = c.examples[0]
    with pytest.raises(ValueError):
        await JevClient(
            "key", protocol="openai", retries=0, transport=transport
        ).evaluate([page], {page.slide_id: [c]})


@pytest.mark.asyncio
async def test_settings_roundtrip_and_unsaved_test_reuse_key_without_leaking_or_saving(
    monkeypatch,
):
    settings = {"jev_api_key": "saved-secret"}
    reads, writes, requests = [], [], []

    async def read(user_id):
        reads.append(user_id)
        return dict(settings)

    async def write(values, user_id):
        writes.append(user_id)
        settings.update(values)
        return True

    def handle(request):
        requests.append(request)
        data = json.loads(request.content)
        assert data["model"] == "private-decision-model"
        assert request.headers["authorization"] == "Bearer saved-secret"
        return httpx.Response(
            200, json={"answers": {"q0": {"type": "noul", "noul": 0.9}}}
        )

    monkeypatch.setattr(
        db_config_service,
        "get_db_config_service",
        lambda: SimpleNamespace(get_all_config=read, update_config=write),
    )
    monkeypatch.setattr(
        api,
        "JevClient",
        lambda *a, **kw: JevClient(*a, **kw, transport=httpx.MockTransport(handle)),
    )
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user_required] = lambda: SimpleNamespace(
        id=7
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        original = (
            await client.get("/api/global-master-templates/packages/settings")
        ).json()
        assert (
            original["endpoint_url"] == DEFAULT_DECISION_ENDPOINT
            and original["protocol"] == "jev"
        )
        payload = {
            "enabled": True,
            "model": "private-decision-model",
            "endpoint_url": "https://decision.test/custom/score",
        }
        result = await client.post(
            "/api/global-master-templates/packages/settings/test", json=payload
        )
        assert result.status_code == 200 and result.json()["success"]
        assert not writes and len(requests) == 1
        assert (
            await client.put(
                "/api/global-master-templates/packages/settings", json=payload
            )
        ).status_code == 200
        readback = await client.get("/api/global-master-templates/packages/settings")
        assert readback.json()["endpoint_url"] == payload["endpoint_url"]
        assert readback.json()["model"] == payload["model"]
        assert "saved-secret" not in readback.text and "saved-secret" not in result.text
        assert settings["jev_api_key"] == "saved-secret" and set(reads + writes) == {7}


@pytest.mark.asyncio
async def test_connection_test_reports_auth_error_without_upstream_secrets(monkeypatch):
    monkeypatch.setattr(
        db_config_service,
        "get_db_config_service",
        lambda: SimpleNamespace(get_all_config=AsyncMock(return_value={})),
    )
    monkeypatch.setattr(
        api,
        "JevClient",
        lambda *a, **kw: JevClient(
            *a,
            **kw,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(401, text="secret-from-upstream")
            ),
        ),
    )
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user_required] = lambda: SimpleNamespace(
        id=7
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/global-master-templates/packages/settings/test",
            json={"api_key": "key"},
        )
        assert response.status_code == 502 and "认证失败" in response.json()["detail"]
        assert "secret-from-upstream" not in response.text


@pytest.mark.asyncio
async def test_generation_uses_saved_protocol_endpoint_and_model(monkeypatch):
    calls = []
    c = load_builtin_package().components[0]
    page = c.examples[0]

    def factory(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            evaluate=AsyncMock(return_value=({(page.slide_id, c.id): 1.0}, []))
        )

    monkeypatch.setattr(selector, "JevClient", factory)
    await selector.select_components(
        load_builtin_package().model_copy(update={"components": (c,)}),
        [page],
        PackageOptions(),
        {
            "jev_enabled": True,
            "jev_api_key": "key",
            "jev_model": "custom",
            "jev_protocol": "openai",
            "jev_endpoint_url": "https://decision.test/api",
        },
        SimpleNamespace(),
    )
    assert calls[0][0][1] == "custom"
    assert calls[0][1]["endpoint_url"] == "https://decision.test/api"
    assert calls[0][1]["protocol"] == "openai"
