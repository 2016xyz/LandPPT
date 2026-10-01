"""User-scoped package versions. Publishing never rewrites an existing manifest."""

import asyncio

from sqlalchemy import delete, func, or_, select, update

from ...database.database import AsyncSessionLocal
from ...database.models import (
    GlobalMasterTemplate,
    PackageProjectState,
    TemplatePackageVersion,
)
from .builtin import load_builtin_package
from .schemas import TemplatePackage
from .service import validate_package


class PackageNotFound(ValueError):
    pass


class PackageConflict(ValueError):
    pass


class PackageCatalog:
    def __init__(self, user_id: int, session_factory=None):
        if not user_id:
            raise ValueError("an authenticated user is required")
        self.user_id = user_id
        self.sessions = session_factory or AsyncSessionLocal

    def scope(self):
        # Deleted packages still referenced by projects are hidden, not removed.
        return (
            or_(
                GlobalMasterTemplate.user_id == self.user_id,
                GlobalMasterTemplate.user_id.is_(None),
            )
            & GlobalMasterTemplate.is_active.isnot(False)
            & (GlobalMasterTemplate.template_kind == "package")
        )

    @staticmethod
    def payload(version, template):
        return {
            "id": version.id,
            "template_id": template.id,
            "template_name": template.template_name,
            "description": (
                template.description
                if template.description is not None
                else version.manifest.get("description", "")
            ),
            "template_kind": "package",
            "version": version.version,
            "status": version.status,
            "content_hash": version.content_hash,
            "manifest": version.manifest,
            "validation_report": version.validation_report,
            "user_id": template.user_id,
            "editable": bool(
                template.user_id and version.status in {"draft", "validated"}
            ),
        }

    async def list(self):
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(TemplatePackageVersion, GlobalMasterTemplate)
                    .join(
                        GlobalMasterTemplate,
                        GlobalMasterTemplate.id == TemplatePackageVersion.template_id,
                    )
                    .where(
                        self.scope(),
                        or_(
                            GlobalMasterTemplate.user_id == self.user_id,
                            TemplatePackageVersion.status == "published",
                        ),
                    )
                    .order_by(
                        GlobalMasterTemplate.id, TemplatePackageVersion.version.desc()
                    )
                )
            ).all()
            return [self.payload(version, template) for version, template in rows]

    async def get(self, version_id: int):
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(TemplatePackageVersion, GlobalMasterTemplate)
                    .join(
                        GlobalMasterTemplate,
                        GlobalMasterTemplate.id == TemplatePackageVersion.template_id,
                    )
                    .where(TemplatePackageVersion.id == version_id, self.scope())
                )
            ).first()
            if not row or (
                row[1].user_id != self.user_id
                and row[0].status not in {"published", "retired"}
            ):
                raise PackageNotFound("模板包版本不存在")
            return self.payload(*row)

    async def create(self, manifest: TemplatePackage, template_id: int | None = None):
        async with self.sessions() as session, session.begin():
            if template_id is None:
                template = GlobalMasterTemplate(
                    user_id=self.user_id,
                    template_name=manifest.name,
                    template_kind="package",
                    description=manifest.description,
                    html_template="",
                    tags=["模板包"],
                    is_default=False,
                    created_by=f"user:{self.user_id}",
                )
                session.add(template)
                await session.flush()
                version_number = 1
            else:
                template = await self._owned_template(session, template_id)
                versions = (
                    await session.scalars(
                        select(TemplatePackageVersion)
                        .where(TemplatePackageVersion.template_id == template.id)
                        .order_by(TemplatePackageVersion.version.desc())
                    )
                ).all()
                version_number = versions[0].version + 1 if versions else 1
                if (
                    versions
                    and manifest.package_id != versions[0].manifest["package_id"]
                ):
                    raise PackageConflict("新版本必须保留模板包标识")
            manifest = TemplatePackage.model_validate(
                {
                    **manifest.model_dump(),
                    "version": version_number,
                    "name": template.template_name,
                    "description": (
                        template.description
                        if template.description is not None
                        else manifest.description
                    ),
                }
            )
            row = TemplatePackageVersion(
                template_id=template.id,
                version=version_number,
                status="draft",
                manifest=manifest.model_dump(mode="json"),
                content_hash=manifest.content_hash(),
            )
            session.add(row)
            await session.flush()
            return self.payload(row, template)

    async def transition(self, version_id: int, action: str):
        snapshot = await self.get(version_id)
        if snapshot["user_id"] != self.user_id:
            raise PackageNotFound("不能修改系统模板包")
        report = None
        if action in {"validate", "publish"}:
            report = await asyncio.to_thread(
                validate_package, TemplatePackage.model_validate(snapshot["manifest"])
            )
        async with self.sessions() as session, session.begin():
            await self._owned_template(session, snapshot["template_id"])
            row = await session.get(
                TemplatePackageVersion, version_id, with_for_update=True
            )
            if row is None:
                raise PackageNotFound("模板包版本不存在")
            if row.content_hash != snapshot["content_hash"]:
                raise PackageConflict("版本已发生变化，请重新校验")
            allowed = {
                "validate": {"draft", "validated"},
                "publish": {"draft", "validated"},
                "retire": {"published"},
            }
            if action not in allowed or row.status not in allowed[action]:
                raise PackageConflict("不允许此版本状态转换")
            row.status = {
                "validate": "validated",
                "publish": "published",
                "retire": "retired",
            }[action]
            if report is not None:
                row.validation_report = report
        return await self.get(version_id)

    async def install_builtin(self):
        for row in await self.list():
            if (
                row["user_id"] == self.user_id
                and row["manifest"]["package_id"] == "editorial"
                and row["status"] == "published"
            ):
                return row
        row = await self.create(load_builtin_package())
        return await self.transition(row["id"], "publish")

    async def _owned_template(self, session, template_id):
        # Serialize all catalog mutations in the same parent-then-version order,
        # including SQLite, where SELECT FOR UPDATE has no locking effect.
        await session.execute(
            update(GlobalMasterTemplate)
            .where(
                GlobalMasterTemplate.id == template_id,
                GlobalMasterTemplate.user_id == self.user_id,
                GlobalMasterTemplate.template_kind == "package",
                GlobalMasterTemplate.is_active.isnot(False),
            )
            .values(updated_at=GlobalMasterTemplate.updated_at)
        )
        template = await session.scalar(
            select(GlobalMasterTemplate)
            .where(
                GlobalMasterTemplate.id == template_id,
                GlobalMasterTemplate.user_id == self.user_id,
                GlobalMasterTemplate.template_kind == "package",
                GlobalMasterTemplate.is_active.isnot(False),
            )
            .with_for_update()
        )
        if not template:
            raise PackageNotFound("模板包不存在或不可修改")
        return template

    async def editable_draft(self, version_id: int):
        """The draft that edits go to: itself if editable, otherwise a new draft."""
        source = await self.get(version_id)
        if source["editable"]:
            return source
        manifest = TemplatePackage.model_validate(source["manifest"])
        # Shared system packages are copied; owned packages receive a new version.
        return await self.create(
            manifest, source["template_id"] if source["user_id"] else None
        )

    async def update_draft(
        self, version_id: int, manifest: TemplatePackage, expected_hash=None
    ):
        """Drafts are mutable workspaces; projects can only use published versions."""
        source = await self.get(version_id)
        if not source["editable"]:
            raise PackageConflict("已发布或停用的版本不可修改，请先创建草稿")
        expected_hash = expected_hash or source["content_hash"]
        await asyncio.to_thread(validate_package, manifest)
        async with self.sessions() as session, session.begin():
            template = await self._owned_template(session, source["template_id"])
            row = await session.get(
                TemplatePackageVersion, version_id, with_for_update=True
            )
            if row is None:
                raise PackageNotFound("模板包版本不存在")
            if row.status not in {"draft", "validated"}:
                raise PackageConflict("已发布或停用的版本不可修改，请先创建草稿")
            if expected_hash and row.content_hash != expected_hash:
                raise PackageConflict("草稿已在其他窗口修改，请刷新后重试")
            if manifest.package_id != row.manifest["package_id"]:
                raise PackageConflict("草稿必须保留模板包标识")
            manifest = TemplatePackage.model_validate(
                {
                    **manifest.model_dump(),
                    "version": row.version,
                    "name": template.template_name,
                    "description": (
                        template.description
                        if template.description is not None
                        else manifest.description
                    ),
                }
            )
            row.manifest = manifest.model_dump(mode="json")
            row.content_hash = manifest.content_hash()
            row.status, row.validation_report = "draft", None
            await session.flush()
            return self.payload(row, template)

    async def mutate_draft(self, version_id: int, mutate, expected_hash=None):
        """Apply a component-list change to a draft after full-package validation."""
        source = await self.get(version_id)
        if not source["editable"]:
            raise PackageConflict("已发布或停用的版本不可修改，请先创建草稿")
        if expected_hash and source["content_hash"] != expected_hash:
            raise PackageConflict("草稿已在其他窗口修改，请刷新后重试")
        package = TemplatePackage.model_validate(source["manifest"])
        components = mutate(list(package.components))
        if not 1 <= len(components) <= 40:
            raise PackageConflict("模板包必须保留 1–40 个版式")
        candidate = TemplatePackage.model_validate(
            {**package.model_dump(), "components": tuple(components)}
        )
        return await self.update_draft(version_id, candidate, source["content_hash"])

    async def _references(self, session, version_ids):
        if not version_ids:
            return 0
        return await session.scalar(
            select(func.count())
            .select_from(PackageProjectState)
            .where(PackageProjectState.version_id.in_(version_ids))
        )

    async def rename(self, template_id: int, name: str):
        return await self.update_metadata(template_id, name=name)

    async def update_metadata(
        self, template_id: int, name: str | None = None, description: str | None = None
    ):
        changes = {}
        if name is not None:
            name = name.strip()
            if not 1 <= len(name) <= 255:
                raise ValueError("名称须为 1–255 个字符")
            changes["name"] = name
        if description is not None:
            description = description.strip()
            if len(description) > 2000:
                raise ValueError("描述不能超过 2000 个字符")
            changes["description"] = description
        if not changes:
            raise ValueError("请提供名称或描述")
        async with self.sessions() as session, session.begin():
            template = await self._owned_template(session, template_id)
            if name is not None:
                template.template_name = name
            if description is not None:
                template.description = description
            # Catalog metadata can change; published content snapshots stay immutable.
            drafts = (
                await session.scalars(
                    select(TemplatePackageVersion)
                    .where(
                        TemplatePackageVersion.template_id == template_id,
                        TemplatePackageVersion.status.in_(("draft", "validated")),
                    )
                    .with_for_update()
                )
            ).all()
            for row in drafts:
                manifest = TemplatePackage.model_validate({**row.manifest, **changes})
                row.manifest = manifest.model_dump(mode="json")
                row.content_hash = manifest.content_hash()
                row.status, row.validation_report = "draft", None
            return {
                "template_id": template_id,
                "template_name": template.template_name,
                "description": template.description,
            }

    async def delete_template(self, template_id: int):
        async with self.sessions() as session, session.begin():
            template = await self._owned_template(session, template_id)
            versions = (
                await session.scalars(
                    select(TemplatePackageVersion).where(
                        TemplatePackageVersion.template_id == template_id
                    )
                )
            ).all()
            used = await self._references(session, [v.id for v in versions])
            if used:
                # Referenced versions must stay readable for existing projects.
                template.is_active = False
                for row in versions:
                    if row.status == "published":
                        row.status = "retired"
                return {"deleted": True, "hidden": True, "projects": used}
            await session.execute(
                delete(TemplatePackageVersion).where(
                    TemplatePackageVersion.template_id == template_id
                )
            )
            await session.delete(template)
            return {"deleted": True, "hidden": False, "projects": 0}

    async def delete_version(self, version_id: int):
        source = await self.get(version_id)
        if not source["editable"]:
            raise PackageConflict("只能删除自己的草稿版本；已发布版本请停用")
        async with self.sessions() as session, session.begin():
            template = await self._owned_template(session, source["template_id"])
            row = await session.get(
                TemplatePackageVersion, version_id, with_for_update=True
            )
            if row is None:
                raise PackageNotFound("模板包版本不存在")
            if row.status not in {"draft", "validated"}:
                raise PackageConflict("版本已发布，不能删除草稿")
            if await self._references(session, [version_id]):
                raise PackageConflict("有项目正在使用此版本")
            await session.delete(row)
            await session.flush()
            left = await session.scalar(
                select(func.count())
                .select_from(TemplatePackageVersion)
                .where(TemplatePackageVersion.template_id == template.id)
            )
            if not left:
                await session.delete(template)
        return {"deleted": True}
