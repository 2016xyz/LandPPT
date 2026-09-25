"""The package branch of the existing slide generation workflow."""

import asyncio
import contextlib
import json
import logging
import time

from sqlalchemy.exc import OperationalError

from ....ai import AIMessage, MessageRole
from ....core.config import app_config
from ...db_config_service import get_db_config_service
from ...ppt_image_processor import PPTImageProcessor
from ...template_package.catalog import PackageConflict
from ...template_package.schemas import PageContent, TemplatePackage
from ..svg_page import build_slide_html, extract_svg_markup, process_svg_candidate
from .candidate_filter import (
    all_problems,
    compatible_components,
    demote_metrics,
    display_fields,
    has_structural_fit,
)
from .content_service import ContentService, bounded_completion
from .options import PROMPT_VERSION, PackageOptions
from .renderer import PackageRenderError, RenderedPage, render_page
from .selector import select_components
from .storage import PackageStorage, fingerprint

logger = logging.getLogger(__name__)


def event(kind, **payload):
    return "data: " + json.dumps({"type": kind, **payload}, ensure_ascii=False) + "\n\n"


def describe_error(exc):
    # A bare TimeoutError() stringifies to "", which left failed pages unexplained.
    return str(exc) or type(exc).__name__


def completed_pages(snapshot):
    return sum(
        bool(p["html_content"])
        and (p["status"] == "completed" or p["manual"] or p["locked"])
        for p in snapshot["pages"]
    )


class PackageWorkflow:
    def __init__(self, service, storage=None):
        self.service = service
        self.storage = storage or PackageStorage(service.user_id)
        self.content_service = ContentService(service)
        self.heartbeat = None

    async def _check_cancelled(self, project_id):
        if self.heartbeat is not None and self.heartbeat.done():
            self.heartbeat.result()
        if await self.service._is_slides_generation_cancelled(project_id):
            raise asyncio.CancelledError("已停止生成，已完成的内容会保留")

    async def _heartbeat(self, project_id, token):
        while True:
            await asyncio.sleep(30)
            for attempt in range(5):
                try:
                    await self.storage.lease(project_id, token)
                    break
                except OperationalError:
                    # A fresh transaction per attempt; a busy database must not
                    # silently kill lease renewal for the remainder of the run.
                    if attempt == 4:
                        raise
                    logger.warning("Retrying package lease renewal for %s", project_id)
                    await asyncio.sleep(min(2**attempt, 8))

    async def _phase(self, project_id, phase, message, progress, token):
        await self.storage.progress(
            project_id,
            token,
            "running",
            progress,
            {
                "package_phase": phase,
                "message": message,
                "run_token": token,
                "started_at": self.started_at,
            },
        )

    async def _fallback(self, package, content, assets):
        from ...prompts import prompts_manager

        prompt = "将以下最终成稿绘制为一页 SVG。必须逐字保留全部文字与数据，仅调整布局，不添加事实。图片仅可使用给定地址。\n" + json.dumps(
            {
                "content": content.model_dump(mode="json"),
                "theme": package.theme.model_dump(),
                "images": assets,
            },
            ensure_ascii=False,
        )
        response = await bounded_completion(
            self.service,
            self.service._chat_completion_for_role(
                "slide_generation",
                messages=[
                    AIMessage(
                        role=MessageRole.SYSTEM,
                        content=prompts_manager.get_svg_page_system_prompt(),
                    ),
                    AIMessage(role=MessageRole.USER, content=prompt),
                ],
                temperature=0.3,
                stream_response=True,
            ),
        )
        markup = extract_svg_markup(response.content)
        if not markup:
            raise PackageRenderError(["自由 SVG 回退未返回完整页面"])
        result = await asyncio.to_thread(
            process_svg_candidate, markup, allowed_image_urls=set(assets.values())
        )
        normalized = "".join("".join(result.root.itertext()).split())
        missing = [
            key
            for key, value in display_fields(content).items()
            if not key.startswith("visual_briefs")
            and "".join(value.split()) not in normalized
        ]
        if result.blocking or missing or result.sanitize_issues:
            details = [
                *(f"缺少文字 {key}" for key in missing[:5]),
                *(str(i) for i in list(result.blocking)[:3]),
                *(str(i) for i in list(result.sanitize_issues)[:3]),
            ]
            raise PackageRenderError(
                ["自由 SVG 回退未通过排版或内容完整性检查：" + "；".join(details)]
            )
        return RenderedPage(
            build_slide_html(result.markup, title=content.title, lang=package.language),
            result.markup,
            {
                "generation_mode": "freeform",
                "render_mode": "svg",
                "package_fallback": True,
                "package_hash": package.content_hash(),
                "package_version": package.version,
                "package_id": package.package_id,
                "slide_id": content.slide_id,
                "content_revision": content.content_revision,
                "assets": assets,
                "fallback_usage": response.usage,
                "fallback_model": response.model,
            },
        )

    @staticmethod
    def _content_fits(package, raw_content, options):
        """Saved content that no layout can hold would fail every resume; rewrite it."""
        try:
            content = PageContent.model_validate(raw_content)
        except ValueError:
            return False
        kwargs = {"allow_images": options.allow_images}
        if has_structural_fit(package, content, **kwargs):
            return True
        return demote_metrics(content, package, **kwargs) is not None

    async def render_with_repairs(
        self, project_id, snapshot, page, package, options, component_id, token
    ):
        content = PageContent.model_validate(page["content"])
        revision, assets = page["revision"], dict(page["assets"] or {})
        if content.visual_briefs and any(
            v.id not in assets for v in content.visual_briefs
        ):
            await self._check_cancelled(project_id)
            processor = PPTImageProcessor(
                image_service=self.service.image_service,
                user_id=self.service.user_id,
                provider_override=getattr(self.service, "provider_name", None),
            )
            # Persist the asset result before rendering so retries reuse it.
            missing = content.model_copy(
                update={
                    "visual_briefs": tuple(
                        v for v in content.visual_briefs if v.id not in assets
                    )
                }
            )
            assets.update(
                await processor.process_package_images(
                    missing,
                    snapshot["confirmed_requirements"],
                    page["slide_index"] + 1,
                    len(snapshot["pages"]),
                )
            )
            await self._check_cancelled(project_id)
            revision = await self.storage.save_assets(
                project_id, content.slide_id, revision, assets, token=token
            )
            page.update(revision=revision, assets=assets)
        problems, repair_errors = {}, []
        locked_id = page["component_id"] if page.get("locked") else None
        fit_kwargs = {
            "allow_images": options.allow_images,
            "image_budget": options.image_budget,
        }
        if not locked_id and not has_structural_fit(package, content, **fit_kwargs):
            # Repair may not alter metrics, so a blocks+metrics page with no mixed
            # layout could never converge. Keep every metric as a text block instead.
            demoted = demote_metrics(content, package, **fit_kwargs)
            if demoted is not None:
                await self._check_cancelled(project_id)
                revision, content = await self.storage.save_content(
                    project_id,
                    demoted,
                    revision,
                    page["fingerprint"],
                    {"repair": {"deterministic": "metrics_to_blocks"}},
                    token=token,
                )
                page.update(revision=revision, content=content.model_dump(mode="json"))
        for attempt in range(options.repair_attempts + 1):
            legal = compatible_components(
                package,
                content,
                locked_component_id=locked_id,
                **fit_kwargs,
            )
            ordered = sorted(legal, key=lambda c: c.id != component_id)
            problems = {}
            for candidate in ordered:
                try:
                    rendered = await asyncio.to_thread(
                        render_page,
                        package,
                        candidate.id,
                        content,
                        assets=assets,
                        allowed_image_urls=frozenset(assets.values()),
                    )
                    return revision, rendered
                except PackageRenderError as exc:
                    problems[candidate.id] = list(exc.problems)
            if not legal:
                # Tell the repair why each layout rejects the page, not just "none fit".
                problems = {
                    cid: items
                    for cid, items in all_problems(package, content, **fit_kwargs).items()
                    if locked_id is None or cid == locked_id
                }
            if attempt < options.repair_attempts:
                await self._check_cancelled(project_id)
                # Every candidate's defects: fitting any one of them is enough.
                try:
                    repaired, usage = await self.content_service.repair(
                        content, package, problems or {"*": ["没有容量匹配的版式"]}
                    )
                    # Reject invalid repairs without losing the last valid content.
                    if repaired.visual_briefs != content.visual_briefs:
                        raise PackageRenderError(["内容修复不能更改配图需求"])
                except PackageConflict:
                    raise
                except Exception as exc:
                    repair_errors.append(f"内容修复失败：{describe_error(exc)}")
                    logger.warning(
                        "Package repair failed for %s/%s (attempt %s): %s",
                        project_id,
                        content.slide_id,
                        attempt + 1,
                        describe_error(exc),
                    )
                    continue
                await self._check_cancelled(project_id)
                revision, content = await self.storage.save_content(
                    project_id,
                    repaired,
                    revision,
                    page["fingerprint"],
                    {"repair": usage},
                    token=token,
                )
                page.update(revision=revision, content=content.model_dump(mode="json"))
        if options.allow_freeform_fallback and not page.get("locked"):
            await self._check_cancelled(project_id)
            try:
                return revision, await self._fallback(package, content, assets)
            except PackageConflict:
                raise
            except Exception as exc:
                repair_errors.append(f"自由 SVG 回退失败：{describe_error(exc)}")
        raise PackageRenderError(
            (
                [f"{cid}: {'; '.join(items)}" for cid, items in problems.items()]
                or ["没有容量匹配的版式"]
            )
            + repair_errors
        )

    async def run(self, project_id):
        token, heartbeat = None, None
        self.started_at = time.time()
        try:
            snapshot = await self.storage.snapshot(project_id)
            token = await self.storage.lease(project_id)
            snapshot = await self.storage.snapshot(project_id)
            heartbeat = asyncio.create_task(self._heartbeat(project_id, token))
            self.heartbeat = heartbeat
            package = TemplatePackage.model_validate(snapshot["manifest"])
            options = PackageOptions.model_validate(snapshot["options"])
            config = await get_db_config_service().get_all_config(
                user_id=self.service.user_id
            )
            _, provider_settings = await self.service.get_role_provider_async(
                "slide_generation"
            )
            bill = bool(
                app_config.enable_credits_system
                and provider_settings.get("provider") == "landppt"
            )
            await self._phase(project_id, "content", "正在编写页面内容", 0, token)
            slides = snapshot["outline"]["slides"]
            failures, previous_family = [], None
            # Reserve assets of all saved pages, including pages in later batches.
            reserved = {
                p["slide_id"]: len(p["assets"] or {}) for p in snapshot["pages"]
            }
            used_images = sum(reserved.values())
            total = len(slides)
            for start in range(0, total, options.batch_size):
                await self._check_cancelled(project_id)
                snapshot = await self.storage.snapshot(project_id)
                by_id = {p["slide_id"]: p for p in snapshot["pages"]}
                batch = slides[start : start + options.batch_size]
                requested, hashes, pending = [], {}, []
                for outline in batch:
                    sid = outline["slide_id"]
                    page = by_id[sid]
                    digest = fingerprint(
                        {
                            "slide": outline,
                            "requirements": snapshot["requirements"],
                            "confirmed": snapshot["confirmed_requirements"],
                            "provider": provider_settings,
                            "prompt": PROMPT_VERSION,
                            "allow_images": options.allow_images,
                        }
                    )
                    hashes[sid] = digest
                    same = page["fingerprint"] == digest
                    if page["manual"] or page["locked"]:
                        if not page["html_content"]:
                            failures.append(page["slide_index"] + 1)
                        previous_family = next(
                            (
                                c.family
                                for c in package.components
                                if c.id == page["component_id"]
                            ),
                            previous_family,
                        )
                        continue
                    if (
                        same
                        and page["status"] == "completed"
                        and page["html_content"]
                        and (page["render_metadata"] or {}).get("package_hash")
                        == package.content_hash()
                    ):
                        previous_family = next(
                            (
                                c.family
                                for c in package.components
                                if c.id == page["component_id"]
                            ),
                            previous_family,
                        )
                        continue
                    used_images -= reserved.pop(sid, 0)
                    pending.append(sid)
                    if not same or not page["content"] or not self._content_fits(
                        package, page["content"], options
                    ):
                        requested.append(outline)
                if requested:
                    await self._phase(
                        project_id,
                        "content",
                        f"正在编写第 {start + 1}–{min(start + len(batch), total)} 页内容",
                        start / total * 100,
                        token,
                    )
                    yield event(
                        "progress",
                        completed=completed_pages(snapshot),
                        total=total,
                        message="正在编写页面内容",
                    )
                    try:
                        valid, errors, usage = await self.content_service.expand(
                            snapshot,
                            package,
                            requested,
                            allow_images=options.allow_images
                            and used_images < options.image_budget,
                        )
                    except Exception as exc:
                        valid, errors, usage = (
                            {},
                            {s["slide_id"]: describe_error(exc) for s in requested},
                            {},
                        )
                    # Only failed pages are retried; successful batch content is retained.
                    # Save successful batch replies before any slow retry. A stop
                    # during the retry must not discard other pages' paid content.
                    for outline in sorted(
                        requested, key=lambda item: item["slide_id"] not in valid
                    ):
                        sid = outline["slide_id"]
                        if sid not in valid:
                            await self._check_cancelled(project_id)
                            try:
                                retry, retry_errors, retry_usage = (
                                    await self.content_service.expand(
                                        snapshot,
                                        package,
                                        [outline],
                                        allow_images=options.allow_images
                                        and used_images < options.image_budget,
                                        feedback=errors.get(sid, ""),
                                    )
                                )
                                valid.update(retry)
                                errors.update(retry_errors)
                                usage = {**usage, "retry": retry_usage}
                            except Exception as exc:
                                errors[sid] = describe_error(exc)
                        await self._check_cancelled(project_id)
                        if sid in valid:
                            rev, content = await self.storage.save_content(
                                project_id,
                                valid[sid],
                                by_id[sid]["revision"],
                                hashes[sid],
                                usage,
                                token=token,
                            )
                            by_id[sid].update(
                                revision=rev,
                                content=content.model_dump(mode="json"),
                                fingerprint=hashes[sid],
                            )
                            by_id[sid]["assets"] = {
                                k: v
                                for k, v in (by_id[sid]["assets"] or {}).items()
                                if k in {b.id for b in content.visual_briefs}
                            }
                        else:
                            await self.storage.mark(
                                project_id,
                                sid,
                                by_id[sid]["revision"],
                                error=errors.get(sid, "内容生成失败"),
                                token=token,
                            )
                            failures.append(by_id[sid]["slide_index"] + 1)
                            pending.remove(sid)
                pages = [
                    PageContent.model_validate(by_id[sid]["content"]) for sid in pending
                ]
                remaining = max(0, options.image_budget - used_images)
                batch_options = options.model_copy(update={"image_budget": remaining})
                selectable = [
                    p
                    for p in pages
                    if compatible_components(
                        package,
                        p,
                        allow_images=options.allow_images,
                        image_budget=remaining,
                    )
                ]
                await self._phase(
                    project_id, "layout", "正在匹配页面版式", start / total * 100, token
                )
                choices, audit = {}, {}
                previous_components = [
                    p["component_id"]
                    for p in snapshot["pages"]
                    if p["slide_index"] < start
                    and p["html_content"]
                    and p["component_id"]
                ]
                previous_family = next(
                    (
                        c.family
                        for c in package.components
                        if previous_components and c.id == previous_components[-1]
                    ),
                    None,
                )
                # Saved/locked pages inside this batch are also real neighbours.
                fixed_before, fixed = {}, []
                selectable_ids = {p.slide_id for p in selectable}
                for outline in batch:
                    sid = outline["slide_id"]
                    if sid in selectable_ids:
                        fixed_before[sid], fixed = fixed, []
                    elif (
                        sid not in pending
                        and by_id[sid]["html_content"]
                        and by_id[sid]["component_id"]
                    ):
                        fixed.append(by_id[sid]["component_id"])
                if (
                    selectable
                    and sum(len(p.visual_briefs) for p in selectable) <= remaining
                ):
                    choices, audit = await select_components(
                        package,
                        selectable,
                        batch_options,
                        config,
                        self.content_service,
                        previous_family=previous_family,
                        previous_components=previous_components,
                        fixed_before=fixed_before,
                    )
                for sid in pending:
                    page = by_id[sid]
                    await self._check_cancelled(project_id)
                    try:
                        content = PageContent.model_validate(page["content"])
                        if len(content.visual_briefs) > remaining:
                            raise PackageRenderError(
                                ["全篇配图预算不足，请调整此页配图需求"]
                            )
                        await self._phase(
                            project_id,
                            "render",
                            f"正在排版第 {page['slide_index'] + 1} 页",
                            page["slide_index"] / total * 100,
                            token,
                        )
                        revision, rendered = await self.render_with_repairs(
                            project_id,
                            snapshot,
                            page,
                            package,
                            batch_options.model_copy(
                                update={"image_budget": remaining}
                            ),
                            choices.get(sid),
                            token,
                        )
                        rendered.metadata["selection_report"] = audit
                        await self._check_cancelled(project_id)
                        await self.storage.commit_render(
                            project_id, sid, revision, rendered, token=token, bill=bill
                        )
                        used_images += len(content.visual_briefs)
                        remaining = max(0, options.image_budget - used_images)
                        chosen = next(
                            (
                                c
                                for c in package.components
                                if c.id == rendered.metadata.get("component_id")
                            ),
                            None,
                        )
                        previous_family = chosen.family if chosen else None
                        yield event(
                            "slide",
                            slide_data={
                                "page_number": page["slide_index"] + 1,
                                "slide_id": sid,
                                "title": content.title,
                                "html_content": rendered.html_content,
                                "render_mode": "svg",
                                "metadata": rendered.metadata,
                            },
                        )
                    except PackageConflict:
                        # A concurrent edit or lock wins. Never overwrite it with an error.
                        failures.append(page["slide_index"] + 1)
                    except Exception as exc:
                        # Never label a newer user edit as failed with this task's error.
                        with contextlib.suppress(PackageConflict):
                            await self.storage.mark(
                                project_id,
                                sid,
                                page["revision"],
                                error=describe_error(exc),
                                token=token,
                            )
                        failures.append(page["slide_index"] + 1)
                batch_snapshot = await self.storage.snapshot(project_id)
                yield event(
                    "progress",
                    completed=completed_pages(batch_snapshot),
                    total=total,
                    message="页面检查完成",
                )
            final = await self.storage.snapshot(project_id)
            failures.extend(
                p["slide_index"] + 1
                for p in final["pages"]
                if not p["html_content"]
                or (p["status"] != "completed" and not p["manual"] and not p["locked"])
            )
            # commit_render already updates combined HTML atomically from current rows.
            failed = sorted(set(failures))
            message = (
                f"第 {'、'.join(map(str, failed))} 页未能生成，其余页面已保存；"
                "点击“重新开始”只会重试这些页面"
                if failed
                else "模板包页面生成完成"
            )
            await self.storage.progress(
                project_id,
                token,
                "failed" if failures else "completed",
                completed_pages(final) / total * 100 if total else 100,
                {
                    "failed_pages": failed,
                    "message": message,
                    "completed_at": time.time(),
                },
            )
            yield event(
                "complete",
                total=total,
                succeeded=total - len(failed),
                partial=bool(failures),
                failed_pages=failed,
                message=message,
            )
        except asyncio.CancelledError:
            if token:
                with contextlib.suppress(PackageConflict, OperationalError):
                    await self.storage.progress(
                        project_id,
                        token,
                        "cancelled",
                        None,
                        {"message": "已停止生成，进度已保存"},
                    )
            raise
        except Exception as exc:
            if token:
                with contextlib.suppress(PackageConflict, OperationalError):
                    await self.storage.progress(
                        project_id,
                        token,
                        "failed",
                        None,
                        {"message": describe_error(exc)},
                    )
            yield event("error", message=describe_error(exc))
        finally:
            if heartbeat:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await heartbeat
            if token:
                with contextlib.suppress(PackageConflict, OperationalError):
                    await self.storage.lease(project_id, token, release=True)
            self.heartbeat = None
