"""Fast mode uses real streaming requests and never accepts interrupted output."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from openai import AsyncOpenAI

from landppt.ai.base import AIMessage, AIProvider, MessageRole
from landppt.ai.providers import OpenAIProvider
from landppt.services.runtime.runtime_provider_service import RuntimeProviderService
from landppt.services.slide.package_generation import content_service
from landppt.services.slide.package_generation.content_service import ContentService


class SSEBody(httpx.AsyncByteStream):
    def __init__(self, *, finish="stop", fail=None, wait=False):
        self.finish, self.fail, self.wait = finish, fail, wait
        self.closed = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self):
        # Split both JSON strings and think markers across HTTP/SSE chunks.
        for part in ("<thi", 'nk>内部推理</think>{"pa', 'ges":[{"title":"完整内容"}]}'):
            chunk = {
                "id": "c1",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "grok-4.7",
                "choices": [
                    {"index": 0, "delta": {"content": part}, "finish_reason": None}
                ],
            }
            yield ("data: " + json.dumps(chunk) + "\n\n").encode()
        self.started.set()
        if self.fail:
            raise self.fail
        if self.wait:
            await self.release.wait()
        if self.finish:
            chunk["choices"] = [{"index": 0, "delta": {}, "finish_reason": self.finish}]
            yield ("data: " + json.dumps(chunk) + "\n\n").encode()
        chunk["choices"] = []
        chunk["usage"] = {
            "prompt_tokens": 17,
            "completion_tokens": 9,
            "total_tokens": 26,
        }
        yield ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()

    async def aclose(self):
        self.closed = True


@asynccontextmanager
async def streaming_runtime(body):
    requests = []

    async def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        assert "stream_response" not in payload
        assert "on_chunk" not in payload
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=body
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        async with AsyncOpenAI(
            api_key="test", base_url="https://model.test/v1", http_client=client
        ) as sdk:
            with patch("openai.AsyncOpenAI", return_value=sdk):
                provider = OpenAIProvider(
                    {"model": "grok-4.7", "use_responses_api": False}
                )
            runtime = RuntimeProviderService(SimpleNamespace(user_id=None))
            runtime._get_role_provider_async = AsyncMock(
                return_value=(provider, {"model": "grok-4.7"})
            )
            runtime._get_user_generation_config = AsyncMock(
                return_value={"temperature": 0.5, "top_p": 0.9}
            )
            yield runtime, provider, requests


@pytest.mark.asyncio
async def test_content_json_uses_streaming_http_and_preserves_actual_usage(monkeypatch):
    monkeypatch.setattr(content_service, "llm_timeout", AsyncMock(return_value=10))
    body = SSEBody()
    async with streaming_runtime(body) as (runtime, _, requests):
        data, audit = await ContentService(runtime).json_completion("写内容")
    assert data == {"pages": [{"title": "完整内容"}]}
    assert audit["model"] == "grok-4.7"
    assert audit["usage"]["total_tokens"] == 26 and audit["streamed"]
    assert len(requests) == 1 and requests[0]["temperature"] == 0.3
    assert requests[0]["messages"][0]["role"] == "system"
    assert body.closed


@pytest.mark.asyncio
async def test_chunk_callback_receives_visible_text_before_completion(monkeypatch):
    monkeypatch.setattr(content_service, "llm_timeout", AsyncMock(return_value=10))
    body = SSEBody(wait=True)
    chunks = []

    async def receive(text):
        chunks.append(text)

    async with streaming_runtime(body) as (runtime, _, requests):
        task = asyncio.create_task(
            ContentService(runtime).json_completion("写内容", on_chunk=receive)
        )
        await asyncio.wait_for(body.started.wait(), 2)
        assert not task.done()
        assert "".join(chunks) == '{"pages":[{"title":"完整内容"}]}'
        assert "内部推理" not in "".join(chunks)
        body.release.set()
        data, audit = await task
    assert data["pages"][0]["title"] == "完整内容"
    assert audit["usage"]["total_tokens"] == 26
    assert body.closed and len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", [None, "length", "content_filter"])
async def test_valid_json_without_successful_stream_finish_is_rejected(
    monkeypatch, finish
):
    monkeypatch.setattr(content_service, "llm_timeout", AsyncMock(return_value=2))
    body = SSEBody(finish=finish)
    async with streaming_runtime(body) as (runtime, _, requests):
        with pytest.raises(ValueError, match="未完整结束"):
            await ContentService(runtime).json_completion("写内容")
    assert body.closed and len(requests) == 1


@pytest.mark.asyncio
async def test_network_error_does_not_parse_partial_json_or_silently_retry(monkeypatch):
    monkeypatch.setattr(content_service, "llm_timeout", AsyncMock(return_value=2))
    body = SSEBody(fail=httpx.ReadError("stream interrupted"))
    async with streaming_runtime(body) as (runtime, _, requests):
        with pytest.raises(httpx.ReadError):
            await ContentService(runtime).json_completion("写内容")
    assert body.closed and len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancel_close_the_http_stream(monkeypatch, cancel):
    monkeypatch.setattr(
        content_service, "llm_timeout", AsyncMock(return_value=5 if cancel else 0.05)
    )
    body = SSEBody(wait=True)
    async with streaming_runtime(body) as (runtime, _, _):
        task = asyncio.create_task(ContentService(runtime).json_completion("写内容"))
        await asyncio.wait_for(body.started.wait(), 2)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await task
    assert body.closed


@pytest.mark.asyncio
async def test_regular_runtime_completion_keeps_non_streaming_path():
    runtime = RuntimeProviderService(SimpleNamespace(user_id=None))
    provider = SimpleNamespace(
        chat_completion=AsyncMock(return_value="original"),
        collect_streamed_chat_completion=AsyncMock(),
    )
    runtime._get_role_provider_async = AsyncMock(
        return_value=(provider, {"model": "normal"})
    )
    result = await runtime._chat_completion_for_role(
        "slide_generation", messages=[], temperature=0.3, top_p=1
    )
    assert result == "original"
    provider.collect_streamed_chat_completion.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "incomplete"])
async def test_responses_api_collects_stream_and_checks_status(status):
    response = SimpleNamespace(
        status=status,
        output_text='{"ok":true}',
        model="test-model",
        usage=SimpleNamespace(input_tokens=4, output_tokens=3, total_tokens=7),
    )
    closed = []

    @asynccontextmanager
    async def stream(**kwargs):
        try:
            yield SimpleNamespace(get_final_response=AsyncMock(return_value=response))
        finally:
            closed.append(True)

    with patch(
        "openai.AsyncOpenAI",
        return_value=SimpleNamespace(responses=SimpleNamespace(stream=stream)),
    ):
        provider = OpenAIProvider({"model": "test-model", "use_responses_api": True})
    call = provider.collect_streamed_chat_completion(
        [AIMessage(role=MessageRole.USER, content="hello")]
    )
    if status == "completed":
        result = await call
        assert result.content == '{"ok":true}' and result.usage["total_tokens"] == 7
    else:
        with pytest.raises(ValueError, match="未完成"):
            await call
    assert closed


@pytest.mark.asyncio
async def test_other_providers_collect_strings_without_inventing_usage():
    class TextProvider(AIProvider):
        async def chat_completion(self, *args, **kwargs):
            raise AssertionError("Must use streaming")

        async def text_completion(self, *args, **kwargs):
            raise AssertionError("Must use streaming")

        async def stream_chat_completion(self, *args, **kwargs):
            assert "on_chunk" not in kwargs
            try:
                yield '{"ok":'
                yield "true}"
            finally:
                self.closed = True

    provider = TextProvider({"model": "other-model"})
    received = []

    async def on_chunk(chunk):
        received.append(chunk)

    result = await provider.collect_streamed_chat_completion([], on_chunk=on_chunk)
    assert result.content == '{"ok":true}'
    assert result.usage == {} and not result.metadata["usage_available"]
    assert provider.closed
    assert received == ['{"ok":', "true}"]


@pytest.mark.asyncio
async def test_responses_api_forwards_text_deltas_and_preserves_final_usage():
    closed, received = [], []
    final = SimpleNamespace(
        status="completed",
        output_text='{"reply":"准备调整"}',
        model="test",
        usage=SimpleNamespace(input_tokens=4, output_tokens=3, total_tokens=7),
    )

    class Events:
        async def __aiter__(self):
            yield SimpleNamespace(type="response.reasoning.delta", delta="不展示")
            for delta in ('{"reply":"准备', '调整"}'):
                yield SimpleNamespace(type="response.output_text.delta", delta=delta)

        async def get_final_response(self):
            assert received == ['{"reply":"准备', '调整"}']
            return final

    @asynccontextmanager
    async def stream(**kwargs):
        assert "on_chunk" not in kwargs
        try:
            yield Events()
        finally:
            closed.append(True)

    async def on_chunk(chunk):
        received.append(chunk)

    with patch(
        "openai.AsyncOpenAI",
        return_value=SimpleNamespace(responses=SimpleNamespace(stream=stream)),
    ):
        provider = OpenAIProvider({"model": "test", "use_responses_api": True})
    result = await provider.collect_streamed_chat_completion([], on_chunk=on_chunk)
    assert result.content == final.output_text
    assert result.usage["total_tokens"] == 7 and closed


@pytest.mark.asyncio
async def test_free_svg_fallback_requests_streaming_and_still_checks_render(
    monkeypatch,
):
    from landppt.ai.base import AIResponse
    from landppt.services.slide.package_generation.renderer import render_page
    from landppt.services.slide.package_generation.workflow import PackageWorkflow
    from landppt.services.template_package.builtin import load_builtin_package

    monkeypatch.setattr(content_service, "llm_timeout", AsyncMock(return_value=10))
    package = load_builtin_package()
    component = next(c for c in package.components if c.id == "cover")
    content = component.examples[0]
    rendered = render_page(package, component.id, content)
    service = SimpleNamespace(
        _chat_completion_for_role=AsyncMock(
            return_value=AIResponse(
                content=rendered.svg,
                model="test",
                usage={"total_tokens": 12},
            )
        )
    )
    result = await PackageWorkflow(service, SimpleNamespace())._fallback(
        package, content, {}
    )
    assert (
        service._chat_completion_for_role.await_args.kwargs["stream_response"] is True
    )
    assert result.metadata["fallback_usage"] == {"total_tokens": 12}
    assert content.title in result.html_content
