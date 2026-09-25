"""User-scoped package versions. Publishing never rewrites an existing manifest."""

import asyncio

from sqlalchemy import or_, select, update

from ...database.database import AsyncSessionLocal
from ...database.models import GlobalMasterTemplate, TemplatePackageVersion
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
        return or_(
            GlobalMasterTemplate.user_id == self.user_id,
            GlobalMasterTemplate.user_id.is_(None),
        )

    @staticmethod
    def payload(version, template):
        return {
            "id": version.id,
            "template_id": template.id,
            "template_name": template.template_name,
            "template_kind": "package",
            "version": version.version,
            "status": version.status,
            "content_hash": version.content_hash,
            "manifest": version.manifest,
            "validation_report": version.validation_report,
            "user_id": template.user_id,
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
                await session.execute(
                    update(GlobalMasterTemplate)
                    .where(
                        GlobalMasterTemplate.id == template_id,
                        GlobalMasterTemplate.user_id == self.user_id,
                    )
                    .values(updated_at=GlobalMasterTemplate.updated_at)
                )
                template = await session.scalar(
                    select(GlobalMasterTemplate)
                    .where(
                        GlobalMasterTemplate.id == template_id,
                        GlobalMasterTemplate.user_id == self.user_id,
                        GlobalMasterTemplate.template_kind == "package",
                    )
                    .with_for_update()
                )
                if not template:
                    raise PackageNotFound("模板包不存在或不可修改")
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
                {**manifest.model_dump(), "version": version_number}
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
            row = await session.get(
                TemplatePackageVersion, version_id, with_for_update=True
            )
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
