"""Forward user-facing reply deltas without exposing the generated JSON or SVG."""

import asyncio
import json
import re
from contextlib import aclosing


class ReplyPrefix:
    """Decode the leading reply string, including split JSON escapes and emoji."""

    def __init__(self):
        self.buffer = ""
        self.text = ""
        self.finished = False

    def feed(self, chunk):
        if self.finished:
            return ""
        self.buffer += chunk
        start = self.buffer.find("{")
        if start < 0:
            self.buffer = self.buffer[-200:]
            return ""
        if self.buffer[:start].strip() not in {"", "```", "```json"}:
            self.finished = True
            self.buffer = ""
            return ""
        self.buffer = self.buffer[start:]
        match = re.match(r'\{\s*"reply"\s*:\s*"', self.buffer)
        if not match:
            # A non-leading reply is still shown after the final JSON is parsed.
            key = re.match(r'\{\s*"([^"]+)"\s*:', self.buffer)
            if key and key[1] != "reply":
                self.finished = True
                self.buffer = ""
            elif len(self.buffer) > 32768:
                self.finished = True
                self.buffer = ""
            return ""
        raw = self.buffer[match.end() :]
        index = 0
        while index < len(raw):
            if raw[index] == '"':
                self.finished = True
                break
            if raw[index] == "\\":
                if index + 1 >= len(raw):
                    break
                size = 6 if raw[index + 1] == "u" else 2
                if index + size > len(raw):
                    break
                index += size
            else:
                index += 1
        try:
            text = json.loads('"' + raw[:index] + '"')
        except ValueError:
            return ""
        # Defer half of a surrogate pair until its low surrogate arrives.
        if text and 0xD800 <= ord(text[-1]) <= 0xDBFF:
            text = text[:-1]
        if len(text) > 4000:
            raise ValueError("回复须为不超过 4000 字的文字")
        try:
            text.encode("utf-8")
        except UnicodeError:
            return ""
        delta = text[len(self.text) :]
        self.text = text
        if self.finished:
            self.buffer = ""
        return delta


async def reply_completion(content, prompt, *, segment_id, emit=None, **kwargs):
    if emit is None:
        return await content.json_completion(prompt, **kwargs)
    prefix = ReplyPrefix()
    await emit({"type": "reply", "segment_id": segment_id, "text": "", "reset": True})

    async def on_chunk(chunk):
        delta = prefix.feed(chunk)
        if delta:
            await emit({"type": "reply", "segment_id": segment_id, "text": delta})

    result = await content.json_completion(prompt, on_chunk=on_chunk, **kwargs)
    data, _ = result
    reply = data.get("reply", "") if isinstance(data, dict) else ""
    if not isinstance(reply, str) or len(reply) > 4000:
        raise ValueError("回复须为不超过 4000 字的文字")
    try:
        reply.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("回复包含无效字符") from exc
    if reply != prefix.text:
        await emit(
            {"type": "reply", "segment_id": segment_id, "text": reply, "reset": True}
        )
    return result


async def stream_replies(make_events):
    """Merge model callbacks with page events; disconnect cancels the model task."""
    queue = asyncio.Queue(maxsize=16)

    async def emit(item):
        await queue.put(item)

    async def produce():
        try:
            async with aclosing(make_events(emit)) as events:
                async for item in events:
                    await emit(item)
        except Exception as exc:
            await queue.put(exc)
        finally:
            if not asyncio.current_task().cancelling():
                await queue.put(None)

    task = asyncio.create_task(produce())
    try:
        while (item := await queue.get()) is not None:
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
