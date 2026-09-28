"""Authenticated uploads for raster-backed template packages."""

import asyncio
import json
import threading
from functools import partial

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from ..auth.middleware import get_current_user_required
from ..services.slide.package_generation.content_service import llm_timeout
from ..services.template_package.archive import MAX_ARCHIVE_BYTES, import_archive
from ..services.template_package.catalog import PackageCatalog
from ..services.template_package.pptx_analysis import classify_analysis
from ..services.template_package.pptx_import import (
    MAX_UPLOAD,
    ImportOptions,
    analyze_pptx,
    import_pptx,
)
from ..services.template_package.pptx_repair import repair_layout
from ..services.template_package.pptx_vision import reconstruct_layout
from ..services.template_package.service import validate_package

router = APIRouter(tags=["Template package imports"])
render_slots = asyncio.Semaphore(2)


def analysis_service(user_id):
    from ..services.service_instances import get_ppt_service_for_user

    return get_ppt_service_for_user(user_id)


def stream_operation(operation):
    """Upload response streams real phase events while CPU/model work runs."""

    async def events():
        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()

        def progress(stage, message):
            event = {
                "type": "progress",
                "stage": stage,
                "message": message,
            }
            if threading.get_ident() == loop_thread:
                queue.put_nowait(event)
            else:
                loop.call_soon_threadsafe(queue.put_nowait, event)

        async def work():
            try:
                result = await operation(progress)
                await queue.put({"type": "complete", "result": result})
            except Exception as exc:
                await queue.put(
                    {"type": "error", "message": str(exc) or "导入失败，请检查服务日志"}
                )

        task = asyncio.create_task(work())
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), 10)
                except asyncio.TimeoutError:
                    event = {"type": "heartbeat"}
                yield json.dumps(event, ensure_ascii=False) + "\n"
                if event["type"] in {"complete", "error"}:
                    break
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def run_render(function, *args):
    # A disconnected client must not release its slot while the thread still runs.
    await render_slots.acquire()
    task = asyncio.create_task(asyncio.to_thread(function, *args))

    def finished(result):
        render_slots.release()
        if not result.cancelled():
            result.exception()

    task.add_done_callback(finished)
    return await asyncio.shield(task)


async def upload_bytes(file, maximum):
    try:
        data = await file.read(maximum + 1)
    finally:
        await file.close()
    if len(data) > maximum:
        raise HTTPException(413, "上传文件超过大小限制")
    return data


async def import_with_ai_repair(data, settings, service, progress):
    """Bridge the rendering thread to the request loop, including cancellation."""
    loop = asyncio.get_running_loop()
    pending = set()
    lock = threading.Lock()
    stopped = threading.Event()
    visual = settings is not None and settings.mode == "visual"
    model_timeout = await llm_timeout(service) if visual else 180

    def call_model(coroutine_factory):
        with lock:
            if stopped.is_set():
                raise ValueError("导入请求已取消")
            future = asyncio.run_coroutine_threadsafe(coroutine_factory(), loop)
            pending.add(future)
        try:
            return future.result(timeout=model_timeout)
        except TimeoutError as exc:
            raise ValueError(
                f"AI 请求超过 {model_timeout:g} 秒，未保存未通过校验的版式"
            ) from exc
        except Exception as exc:
            raise ValueError(f"AI 修复未完成：{exc}") from exc
        finally:
            future.cancel()
            with lock:
                pending.discard(future)

    def repair(component, error):
        return call_model(lambda: repair_layout(component, error, service))

    def reconstruct(component, error, reference_url):
        return call_model(
            lambda: reconstruct_layout(component, error, reference_url, service)
        )

    try:
        return await run_render(
            partial(
                import_pptx,
                progress=progress,
                repair=repair,
                **(
                    {"reconstruct": reconstruct}
                    if settings is not None and settings.mode == "visual"
                    else {}
                ),
            ),
            data,
            settings,
        )
    finally:
        with lock:
            stopped.set()
            for future in pending:
                future.cancel()


@router.post("/api/global-master-templates/packages-pptx/analyze")
async def analyze(
    file: UploadFile = File(...),
    user=Depends(get_current_user_required),
    stream: bool = False,
    vision: bool = False,
):
    data = await upload_bytes(file, MAX_UPLOAD)

    async def operation(progress):
        progress("queued", "文件上传完成，正在等待渲染资源")
        result = await run_render(
            partial(analyze_pptx, progress=progress, vision=vision), data
        )
        return await classify_analysis(
            result, analysis_service(user.id), progress, vision=vision
        )

    if stream:
        return stream_operation(operation)
    try:
        return await operation(lambda *_: None)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/global-master-templates/packages-pptx/import")
async def create_from_pptx(
    file: UploadFile = File(...),
    options: str = Form(...),
    user=Depends(get_current_user_required),
    stream: bool = False,
):
    data = await upload_bytes(file, MAX_UPLOAD)
    try:
        settings = ImportOptions.model_validate(json.loads(options))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    async def operation(progress):
        progress("queued", "文件上传完成，正在等待渲染资源")
        package, report = await import_with_ai_repair(
            data, settings, analysis_service(user.id), progress
        )
        progress("save", "校验完成，正在保存模板包草稿")
        saved = await PackageCatalog(user.id).create(package)
        return {"package": saved, **report}

    if stream:
        return stream_operation(operation)
    try:
        return await operation(lambda *_: None)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/global-master-templates/packages-import")
async def create_from_archive(
    file: UploadFile = File(...), user=Depends(get_current_user_required)
):
    data = await upload_bytes(file, MAX_ARCHIVE_BYTES)
    try:
        package = await asyncio.to_thread(import_archive, data)
        await asyncio.to_thread(validate_package, package)
        return await PackageCatalog(user.id).create(package)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
