"""Short, owner-scoped transactions for resumable package generation and edits."""

import copy
import hashlib
import html
import json
import re
import time
import uuid

from sqlalchemy import delete, select, update

from ....database.database import AsyncSessionLocal
from ....database.models import (
    CreditTransaction,
    GlobalMasterTemplate,
    NarrationAudio,
    PackagePageState,
    PackageProjectState,
    Project,
    SlideContentRevision,
    SlideData,
    SpeechScript,
    TemplatePackageVersion,
    TodoBoard,
    TodoStage,
    User,
)
from ...template_package.catalog import PackageCatalog, PackageConflict, PackageNotFound
from ...template_package.schemas import PageContent
from .db_retry import retry_transaction


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def project_inputs(project):
    return fingerprint(
        {
            "outline": project.outline,
            "requirements": project.requirements,
            "confirmed": project.confirmed_requirements,
            "version": (project.project_metadata or {}).get(
                "selected_package_version_id"
            ),
        }
    )


class PackageStorage:
    def __init__(self, user_id, session_factory=None):
        if not user_id:
            raise ValueError("an authenticated user is required")
        self.user_id = user_id
        self.sessions = session_factory or AsyncSessionLocal

    async def _project(self, session, project_id, *, write=False):
        where = (Project.project_id == project_id, Project.user_id == self.user_id)
        if write:
            # This obtains a write lock on both SQLite and PostgreSQL. Never hold it
            # across model/network calls. Editors and generators use the same order.
            await session.execute(
                update(Project).where(*where).values(updated_at=Project.updated_at)
            )
        project = await session.scalar(
            select(Project).where(*where).with_for_update()
            if write
            else select(Project).where(*where)
        )
        if not project:
            raise PackageNotFound("项目不存在")
        return project

    @retry_transaction
    async def select_package(self, project_id, version_id, options):
        catalog = PackageCatalog(self.user_id, self.sessions)
        version = await catalog.get(version_id)
        if version["status"] != "published":
            raise PackageConflict("只能选择已发布的模板包版本")
        async with self.sessions() as session, session.begin():
            project = await self._project(session, project_id, write=True)
            # Serialize selecting a package with catalog deletion/retirement.
            template = await session.scalar(
                select(GlobalMasterTemplate)
                .where(GlobalMasterTemplate.id == version["template_id"])
                .with_for_update()
            )
            current = await session.get(TemplatePackageVersion, version_id)
            if (
                not template
                or template.is_active is False
                or not current
                or current.status != "published"
            ):
                raise PackageConflict("模板包版本已停用")
            state = await session.get(PackageProjectState, project_id)
            if state and state.run_token and state.lease_until > time.time():
                raise PackageConflict("请先停止当前生成任务，再切换模板")
            if state is None:
                state = PackageProjectState(
                    project_id=project_id, version_id=version_id
                )
                session.add(state)
            state.version_id = version_id
            state.options = options.model_dump()
            state.run_token, state.lease_until = None, 0
            project.project_metadata = {
                **(project.project_metadata or {}),
                "generation_mode": "package",
                "template_mode": "package",
                "render_mode": "svg",
                "selected_global_template_id": version["template_id"],
                "selected_package_version_id": version_id,
            }
            await self._align_pages(session, project, reset_layout=True)
        return await self.snapshot(project_id)

    async def _align_pages(self, session, project, *, reset_layout=False):
        outline = copy.deepcopy(project.outline or {})
        slides = outline.get("slides", [])
        if not slides:
            raise PackageConflict("请先确认大纲")
        pages = {
            p.slide_id: p
            for p in (
                await session.scalars(
                    select(PackagePageState).where(
                        PackagePageState.project_id == project.project_id
                    )
                )
            ).all()
        }
        rows = (
            await session.scalars(
                select(SlideData).where(SlideData.project_id == project.project_id)
            )
        ).all()
        by_id, by_index = {s.slide_id: s for s in rows}, {
            s.slide_index: s for s in rows
        }
        seen = set()
        for index, outline_slide in enumerate(slides):
            sid = outline_slide.get("slide_id")
            original = by_id.get(sid) if isinstance(sid, str) else None
            if not pages and original is None:
                original = by_index.get(index)
                sid = sid or (original.slide_id if original else None)
            if (
                not isinstance(sid, str)
                or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}", sid)
                or sid in seen
            ):
                if isinstance(sid, str) and sid in seen:
                    original = None
                sid = uuid.uuid4().hex
            seen.add(sid)
            outline_slide["slide_id"] = sid
            page = pages.get(sid)
            if page is None:
                page = PackagePageState(
                    project_id=project.project_id,
                    slide_id=sid,
                    slide_index=index,
                    manual=bool(original and original.is_user_edited),
                    locked=bool(
                        original and (original.slide_metadata or {}).get("locked")
                    ),
                )
                session.add(page)
            else:
                if page.slide_index != index:
                    await self._invalidate(
                        session,
                        project.project_id,
                        page.slide_index,
                        content_changed=True,
                    )
                    await self._invalidate(
                        session, project.project_id, index, content_changed=True
                    )
                    page.slide_index = index
                    page.revision += 1
                if reset_layout and not page.manual and not page.locked:
                    page.status, page.component_id = "pending", None
                    page.revision += 1
            if original:
                original.slide_id, original.slide_index = sid, index
        project.outline = outline
        await session.flush()
        await self._refresh_presentation(session, project)

    @retry_transaction
    async def snapshot(self, project_id):
        async with self.sessions() as session:
            project = await self._project(session, project_id)
            state = await session.get(PackageProjectState, project_id)
            if (
                not state
                or (project.project_metadata or {}).get("generation_mode") != "package"
            ):
                raise PackageNotFound("项目未使用模板包")
            version = await session.get(TemplatePackageVersion, state.version_id)
            pages = (
                await session.scalars(
                    select(PackagePageState)
                    .where(PackagePageState.project_id == project_id)
                    .order_by(PackagePageState.slide_index)
                )
            ).all()
            content_rows = (
                await session.scalars(
                    select(SlideContentRevision).where(
                        SlideContentRevision.project_id == project_id
                    )
                )
            ).all()
            contents = {
                (row.slide_id, row.revision): row.content for row in content_rows
            }
            slides = (
                await session.scalars(
                    select(SlideData).where(SlideData.project_id == project_id)
                )
            ).all()
            rendered = {row.slide_id: row for row in slides}
            active_ids = {
                slide.get("slide_id")
                for slide in (project.outline or {}).get("slides", [])
            }
            return {
                "project_id": project_id,
                "title": project.title,
                "topic": project.topic,
                "requirements": project.requirements,
                "confirmed_requirements": project.confirmed_requirements or {},
                "outline": project.outline,
                "metadata": project.project_metadata or {},
                "version_id": state.version_id,
                "manifest": version.manifest,
                "options": state.options,
                "running": bool(state.run_token and state.lease_until > time.time()),
                "pages": [
                    {
                        "slide_id": p.slide_id,
                        "slide_index": p.slide_index,
                        "revision": p.revision,
                        "content_revision": p.content_revision,
                        "fingerprint": p.fingerprint,
                        "component_id": p.component_id,
                        "assets": p.assets,
                        "status": p.status,
                        "error": p.error,
                        "manual": p.manual
                        or bool(
                            rendered.get(p.slide_id)
                            and rendered[p.slide_id].is_user_edited
                        ),
                        "locked": p.locked
                        or bool(
                            rendered.get(p.slide_id)
                            and (rendered[p.slide_id].slide_metadata or {}).get(
                                "locked"
                            )
                        ),
                        "content": contents.get((p.slide_id, p.content_revision)),
                        "html_content": (
                            rendered[p.slide_id].html_content
                            if p.slide_id in rendered
                            else None
                        ),
                        "render_metadata": (
                            rendered[p.slide_id].slide_metadata
                            if p.slide_id in rendered
                            else {}
                        ),
                    }
                    for p in pages
                    if p.slide_id in active_ids
                ],
            }

    @retry_transaction
    async def lease(self, project_id, token=None, *, release=False):
        async with self.sessions() as session, session.begin():
            project = await self._project(session, project_id, write=True)
            state = await session.get(PackageProjectState, project_id)
            if not state:
                raise PackageNotFound("项目未使用模板包")
            if (
                not release
                and (project.project_metadata or {}).get("generation_mode") != "package"
            ):
                raise PackageConflict("项目生成模式已改变")
            now = time.time()
            if token:
                if state.run_token != token or (
                    not release and state.lease_until < now
                ):
                    raise PackageConflict("生成任务已失效")
                state.run_token = None if release else token
                state.lease_until = 0 if release else now + 300
                return token
            if state.run_token and state.lease_until > now:
                raise PackageConflict("已有生成任务正在运行")
            await self._align_pages(session, project)
            state.run_token, state.lease_until = str(uuid.uuid4()), now + 300
            project.project_metadata = {
                **(project.project_metadata or {}),
                "package_run_input_hash": project_inputs(project),
            }
            return state.run_token

    @retry_transaction
    async def progress(self, project_id, token, status, progress, result):
        """Publish progress only while this run still owns the project."""
        async with self.sessions() as session, session.begin():
            project = await self._project(session, project_id, write=True)
            state = await session.get(PackageProjectState, project_id)
            if (
                not state
                or state.run_token != token
                or state.lease_until < time.time()
                or (project.project_metadata or {}).get("generation_mode") != "package"
            ):
                raise PackageConflict("生成任务已失效")
            if status == "completed" and (project.project_metadata or {}).get(
                "package_run_input_hash"
            ) != project_inputs(project):
                raise PackageConflict("大纲或参考资料已修改，请重新生成")
            values = {"status": status, "result": result, "updated_at": time.time()}
            if progress is not None:
                values["progress"] = progress
            await session.execute(
                update(TodoStage)
                .where(
                    TodoStage.project_id == project_id,
                    TodoStage.stage_id == "ppt_creation",
                )
                .values(**values)
            )
            stages = (
                await session.scalars(
                    select(TodoStage)
                    .where(TodoStage.project_id == project_id)
                    .order_by(TodoStage.stage_index)
                )
            ).all()
            if stages:
                from ....database.service import DatabaseService

                current = next(
                    (
                        i
                        for i, stage in enumerate(stages)
                        if stage.status != "completed"
                    ),
                    len(stages) - 1,
                )
                await session.execute(
                    update(TodoBoard)
                    .where(TodoBoard.project_id == project_id)
                    .values(
                        overall_progress=DatabaseService._calculate_overall_progress(
                            stages
                        ),
                        current_stage_index=current,
                        updated_at=time.time(),
                    )
                )
            if status in {"completed", "failed", "cancelled"}:
                project.status = "completed" if status == "completed" else "in_progress"

    async def _page_for_write(
        self, session, project_id, slide_id, revision, token=None
    ):
        project = await self._project(session, project_id, write=True)
        state = await session.get(PackageProjectState, project_id)
        if (
            not state
            or (project.project_metadata or {}).get("generation_mode") != "package"
        ):
            raise PackageConflict("项目生成模式已改变")
        if token and (state.run_token != token or state.lease_until < time.time()):
            raise PackageConflict("生成任务已失效")
        if token and (project.project_metadata or {}).get(
            "package_run_input_hash"
        ) != project_inputs(project):
            raise PackageConflict("大纲或参考资料已修改，请重新生成")
        page = await session.scalar(
            select(PackagePageState).where(
                PackagePageState.project_id == project_id,
                PackagePageState.slide_id == slide_id,
            )
        )
        outline_slides = (project.outline or {}).get("slides", [])
        if (
            not page
            or page.slide_index >= len(outline_slides)
            or outline_slides[page.slide_index].get("slide_id") != slide_id
        ):
            raise PackageConflict("页面结构已改变，请重新选择模板包")
        if page.revision != revision:
            raise PackageConflict("页面已被修改，请刷新后重试")
        slide = await session.scalar(
            select(SlideData)
            .where(SlideData.project_id == project_id, SlideData.slide_id == slide_id)
            .with_for_update()
        )
        if token and (
            page.manual
            or page.locked
            or (
                slide
                and (slide.is_user_edited or (slide.slide_metadata or {}).get("locked"))
            )
        ):
            raise PackageConflict("页面已被手动编辑或锁定")
        return project, state, page, slide

    @retry_transaction
    async def save_content(
        self, project_id, content, expected_revision, input_hash, usage, *, token=None
    ):
        async with self.sessions() as session, session.begin():
            project, state, page, slide = await self._page_for_write(
                session, project_id, content.slide_id, expected_revision, token
            )
            new_revision = page.content_revision + 1
            content = PageContent.model_validate(
                {**content.model_dump(), "content_revision": new_revision}
            )
            session.add(
                SlideContentRevision(
                    project_id=project_id,
                    slide_id=content.slide_id,
                    revision=new_revision,
                    fingerprint=input_hash,
                    content=content.model_dump(mode="json"),
                    usage=usage,
                )
            )
            page.content_revision, page.fingerprint = new_revision, input_hash
            page.revision += 1
            page.status, page.error = "content_ready", None
            page.assets = {
                key: value
                for key, value in (page.assets or {}).items()
                if key in {v.id for v in content.visual_briefs}
            }
            if slide:
                slide.slide_metadata = {
                    **(slide.slide_metadata or {}),
                    "package_content_stale": True,
                }
            await self._invalidate(
                session, project_id, page.slide_index, content_changed=True
            )
            return page.revision, content

    @retry_transaction
    async def save_assets(self, project_id, slide_id, revision, assets, *, token=None):
        async with self.sessions() as session, session.begin():
            _, _, page, _ = await self._page_for_write(
                session, project_id, slide_id, revision, token
            )
            page.assets = dict(assets)
            page.revision += 1
            return page.revision

    @staticmethod
    async def _invalidate(session, project_id, index, *, content_changed):
        # Existing exporters read these rows; deleting stale derived media forces regeneration.
        if content_changed:
            await session.execute(
                delete(SpeechScript).where(
                    SpeechScript.project_id == project_id,
                    SpeechScript.slide_index == index,
                )
            )
            await session.execute(
                delete(NarrationAudio).where(
                    NarrationAudio.project_id == project_id,
                    NarrationAudio.slide_index == index,
                )
            )

    @retry_transaction
    async def commit_render(
        self,
        project_id,
        slide_id,
        revision,
        rendered,
        *,
        token=None,
        bill=False,
        restore=False,
        content_update=None,
        usage=None,
        assets=None,
        operation="slide_generation",
    ):
        async with self.sessions() as session, session.begin():
            project, state, page, slide = await self._page_for_write(
                session, project_id, slide_id, revision, token
            )
            if (page.manual or (slide and slide.is_user_edited)) and not restore:
                raise PackageConflict("请先恢复模板包控制")
            if not token and (
                page.locked or (slide and (slide.slide_metadata or {}).get("locked"))
            ):
                raise PackageConflict("请先解锁页面")
            if content_update is not None:
                if (
                    content_update.slide_id != slide_id
                    or content_update.content_revision != page.content_revision + 1
                ):
                    raise PackageConflict("内容版本不一致")
                page.content_revision += 1
                session.add(
                    SlideContentRevision(
                        project_id=project_id,
                        slide_id=slide_id,
                        revision=page.content_revision,
                        fingerprint=page.fingerprint,
                        content=content_update.model_dump(mode="json"),
                        usage=usage or {},
                    )
                )
                page.assets = {
                    k: v
                    for k, v in (page.assets or {}).items()
                    if k in {b.id for b in content_update.visual_briefs}
                }
                await self._invalidate(
                    session, project_id, page.slide_index, content_changed=True
                )
            if assets is not None:
                page.assets = dict(assets)
            if content_update is not None and not bill:
                # Direct text edits are free; a later layout switch must not charge them.
                page.billed_revision = page.content_revision
            if bill and page.billed_revision < page.content_revision:
                from ...credits_service import CreditsService

                cost = CreditsService.COSTS[operation]
                result = await session.execute(
                    update(User)
                    .where(User.id == self.user_id, User.credits_balance >= cost)
                    .values(credits_balance=User.credits_balance - cost)
                )
                if result.rowcount != 1:
                    raise PackageConflict("积分不足")
                balance = await session.scalar(
                    select(User.credits_balance).where(User.id == self.user_id)
                )
                session.add(
                    CreditTransaction(
                        user_id=self.user_id,
                        amount=-cost,
                        balance_after=balance,
                        transaction_type="consume",
                        description=(
                            "模板包内容修改"
                            if operation == "ai_edit"
                            else "模板包页面生成"
                        ),
                        reference_id=f"package:{page.id}:{page.content_revision}",
                    )
                )
                page.billed_revision = page.content_revision
            if slide:
                page.history = [
                    *(page.history or [])[-9:],
                    {
                        "html_content": slide.html_content,
                        "metadata": slide.slide_metadata,
                        "saved_at": time.time(),
                    },
                ]
            else:
                slide = SlideData(
                    project_id=project_id,
                    slide_id=slide_id,
                    slide_index=page.slide_index,
                    title="",
                    content_type="content",
                    html_content="",
                )
                session.add(slide)
            await session.flush()
            content = await session.scalar(
                select(SlideContentRevision).where(
                    SlideContentRevision.project_id == project_id,
                    SlideContentRevision.slide_id == slide_id,
                    SlideContentRevision.revision == page.content_revision,
                )
            )
            if not content:
                raise PackageConflict("页面内容稿不存在")
            slide.title, slide.html_content = (
                content.content["title"],
                rendered.html_content,
            )
            slide.slide_id = slide_id
            slide.slide_metadata = {
                **rendered.metadata,
                "package_content_stale": False,
                "video_stale": True,
            }
            slide.is_user_edited = False
            page.component_id = rendered.metadata.get("component_id")
            page.revision += 1
            page.status, page.error, page.manual = "completed", None, False
            project.updated_at = time.time()
            await session.flush()
            await self._refresh_presentation(session, project)
            return page.revision

    @staticmethod
    async def _refresh_presentation(session, project):
        active = {s.get("slide_id") for s in (project.outline or {}).get("slides", [])}
        rows = (
            await session.scalars(
                select(SlideData)
                .where(
                    SlideData.project_id == project.project_id,
                    SlideData.slide_id.in_(active),
                )
                .order_by(SlideData.slide_index)
            )
        ).all()
        project.slides_data = [
            {
                "page_number": s.slide_index + 1,
                "slide_id": s.slide_id,
                "title": s.title,
                "html_content": s.html_content,
                "slide_type": s.content_type,
                "metadata": s.slide_metadata,
                "render_mode": (s.slide_metadata or {}).get("render_mode", "svg"),
                "is_user_edited": s.is_user_edited,
            }
            for s in rows
        ]
        from ..slide_document_service import SlideDocumentService

        project.slides_html = SlideDocumentService(None)._combine_slides_to_full_html(
            project.slides_data,
            html.escape(project.title or "演示文稿"),
            persist_files=False,
        )

    @retry_transaction
    async def mark(
        self,
        project_id,
        slide_id,
        revision,
        *,
        error=None,
        manual=None,
        locked=None,
        component_id=None,
        token=None,
    ):
        async with self.sessions() as session, session.begin():
            _, _, page, slide = await self._page_for_write(
                session, project_id, slide_id, revision, token
            )
            if error is not None:
                page.status, page.error = "failed", str(error)[:2000]
            if manual is not None:
                page.manual = manual
                if slide:
                    slide.is_user_edited = manual
            if locked is not None:
                page.locked = locked
                if slide:
                    slide.slide_metadata = {
                        **(slide.slide_metadata or {}),
                        "locked": locked,
                    }
            if component_id is not None:
                page.component_id = component_id
            page.revision += 1
            return page.revision
