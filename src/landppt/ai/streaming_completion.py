"""Collect structured completions over streaming HTTP without accepting partial output."""

from .base import AIResponse


async def collect_openai_completion(provider, messages, **kwargs):
    if not provider.client:
        raise RuntimeError("OpenAI client not available")
    config = provider._merge_config(**kwargs)
    if config.get("tools"):
        raise ValueError("Structured streaming completion does not support tool calls")

    if provider._should_use_responses_api(config):
        request = provider._build_responses_request(
            config, [provider._convert_message_to_responses_input(m) for m in messages]
        )
        async with provider.client.responses.stream(**request) as stream:
            response = await stream.get_final_response()
        if response.status != "completed":
            raise ValueError(f"模型流式响应未完成：{response.status}")
        content = response.output_text or ""
        model = response.model
        raw_usage = getattr(response, "usage", None)
        usage = (
            provider._extract_usage(
                raw_usage,
                prompt_key="input_tokens",
                completion_key="output_tokens",
                total_key="total_tokens",
            )
            if raw_usage is not None
            else {}
        )
        transport = "responses"
    else:
        request = provider._build_chat_completions_request(
            config,
            [provider._convert_message_to_openai(m) for m in messages],
            stream=True,
        )
        request["stream_options"] = {"include_usage": True}
        stream = await provider.client.chat.completions.create(**request)
        parts, usage, finish_reason = [], {}, None
        model = config.get("model") or provider.model
        # close() runs on success, provider errors, timeout and task cancellation.
        try:
            async for chunk in stream:
                model = getattr(chunk, "model", None) or model
                raw_usage = getattr(chunk, "usage", None)
                if raw_usage is not None:
                    usage = provider._extract_usage(
                        raw_usage,
                        prompt_key="prompt_tokens",
                        completion_key="completion_tokens",
                        total_key="total_tokens",
                    )
                for choice in chunk.choices:
                    if choice.index != 0:
                        continue
                    if choice.delta.content:
                        parts.append(choice.delta.content)
                    if choice.finish_reason is not None:
                        finish_reason = choice.finish_reason
            if finish_reason != "stop":
                raise ValueError(
                    f"模型流式响应未完整结束：{finish_reason or '连接提前关闭'}"
                )
        finally:
            await stream.close()
        content = "".join(parts)
        transport = "chat_completions"

    content = provider._filter_think_content(content)
    if not content.strip():
        raise ValueError("模型流式响应为空")
    return AIResponse(
        content=content,
        model=model,
        usage=usage,
        finish_reason="stop",
        metadata={
            "provider": "openai",
            "transport": transport,
            "streamed": True,
            "usage_available": bool(usage),
        },
    )
