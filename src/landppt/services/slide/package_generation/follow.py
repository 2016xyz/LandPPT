"""Reconnectable SSE: watch saved page revisions instead of stale HTML counts."""

import asyncio
import time

from sqlalchemy.exc import OperationalError

from .storage import PackageStorage
from .workflow import completed_pages, event


async def follow_package_generation(service, project_id):
    storage = PackageStorage(service.user_id)
    sent, last_phase, started, observed_run = {}, None, time.monotonic(), False
    read_failures = 0
    while time.monotonic() - started < 10800:
        try:
            snapshot = await storage.snapshot(project_id)
            stage = await service.project_manager.get_stage_status(
                project_id, "ppt_creation"
            )
            read_failures = 0
        except OperationalError:
            read_failures += 1
            if read_failures >= 5:
                yield event(
                    "error",
                    message="暂时无法读取生成进度，已保存的页面不会丢失，请重新连接",
                )
                return
            yield ": keepalive\n\n"
            await asyncio.sleep(min(2 ** (read_failures - 1), 8))
            continue
        observed_run = observed_run or snapshot["running"]
        for page in snapshot["pages"]:
            if page["html_content"] and (
                page["status"] == "completed" or page["manual"] or page["locked"]
            ):
                if sent.get(page["slide_id"]) == page["revision"]:
                    continue
                yield event(
                    "slide",
                    slide_data={
                        "page_number": page["slide_index"] + 1,
                        "slide_id": page["slide_id"],
                        "title": (page["content"] or {}).get("title", ""),
                        "html_content": page["html_content"],
                        "metadata": page["render_metadata"],
                        "render_mode": "svg",
                    },
                )
                sent[page["slide_id"]] = page["revision"]
        if stage:
            result = stage.get("result") or {}
            completed = completed_pages(snapshot)
            phase = (
                result.get("package_phase"),
                result.get("message"),
                completed,
                len(snapshot["pages"]),
            )
            if phase != last_phase:
                yield event(
                    "progress",
                    completed=completed,
                    total=len(snapshot["pages"]),
                    message=phase[1] or "正在准备页面",
                )
                last_phase = phase
            if not snapshot["running"] and (
                observed_run or time.monotonic() - started > 30
            ):
                if stage.get("status") == "completed":
                    yield event(
                        "complete",
                        total=len(snapshot["pages"]),
                        partial=False,
                        message="模板包页面生成完成",
                    )
                    return
                if stage.get("status") in {"failed", "cancelled"}:
                    if stage.get("status") == "failed" and result.get("failed_pages"):
                        yield event(
                            "complete",
                            total=len(snapshot["pages"]),
                            succeeded=completed,
                            partial=True,
                            message=result.get("message")
                            or "部分页面未能生成，其余页面已保存",
                            failed_pages=result["failed_pages"],
                        )
                        return
                    yield event(
                        "error",
                        message=result.get("message") or "生成已停止",
                        failed_pages=result.get("failed_pages", []),
                    )
                    return
                yield event(
                    "error", message="生成任务已中断，重新生成可从已保存的页面继续"
                )
                return
        yield ": keepalive\n\n"
        await asyncio.sleep(1)
    yield event("error", message="生成超时，请重新连接查看进度")
