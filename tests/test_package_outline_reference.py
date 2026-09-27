"""Fast mode chosen at requirement confirmation shapes every outline prompt."""

import pytest

from landppt.services.template_package.builtin import load_builtin_package
from landppt.services.template_package.outline_reference import (
    REFERENCE_KEY,
    build_outline_reference,
    with_package_reference,
)
from landppt.services.template_package.schemas import PageContent
from landppt.web.route_modules import outline_requirements_routes as routes


def test_reference_lists_component_capacities():
    text = build_outline_reference(load_builtin_package())
    assert "快速模式模板包参考" in text
    assert "points_3" in text and "3 个要点（每个≤" in text
    assert "无要点" in text


def test_reference_is_appended_once_and_only_for_package_projects():
    reference = build_outline_reference(load_builtin_package())
    confirmed = {REFERENCE_KEY: reference}
    once = with_package_reference("用户要求", confirmed)
    assert once.startswith("用户要求") and once.count("快速模式模板包参考") == 1
    assert with_package_reference(once, confirmed) == once
    assert with_package_reference(None, confirmed) == reference
    assert with_package_reference("用户要求", {"generation_preference": {}}) == "用户要求"


class _Catalog:
    rows = {}

    def __init__(self, user_id):
        self.user_id = user_id

    async def get(self, version_id):
        if version_id not in self.rows:
            raise routes.PackageNotFound("missing")
        return self.rows[version_id]


@pytest.mark.asyncio
async def test_preference_requires_a_published_version(monkeypatch):
    manifest = load_builtin_package().model_dump(mode="json")
    _Catalog.rows = {
        1: {"id": 1, "status": "published", "template_name": "编辑风", "manifest": manifest},
        2: {"id": 2, "status": "draft", "template_name": "草稿", "manifest": manifest},
    }
    monkeypatch.setattr(routes, "PackageCatalog", _Catalog)
    preference, reference = await routes._load_package_preference(7, 1)
    assert preference == {"mode": "package", "version_id": 1, "name": "编辑风"}
    assert "快速模式模板包参考" in reference
    for version_id in (0, 2, 3):
        with pytest.raises(ValueError):
            await routes._load_package_preference(7, version_id)


class _Project:
    def __init__(self, metadata):
        self.project_id = "p1"
        self.project_metadata = metadata


@pytest.mark.asyncio
async def test_preferred_package_is_bound_once(monkeypatch):
    from landppt.services.slide.package_generation import storage
    from landppt.web.route_modules import project_library_routes as library

    calls = []

    class _Storage:
        def __init__(self, user_id):
            pass

        async def select_package(self, project_id, version_id, options):
            calls.append(version_id)
            if version_id == 9:
                raise ValueError("模板包版本已停用")

    monkeypatch.setattr(storage, "PackageStorage", _Storage)
    pref = {"mode": "package", "version_id": 5}
    assert await library._apply_preferred_package(_Project({}), pref, 1) == ""
    bound = {"generation_mode": "package", "selected_package_version_id": 5}
    assert await library._apply_preferred_package(_Project(bound), pref, 1) == ""
    assert calls == [5]
    notice = await library._apply_preferred_package(
        _Project({}), {"mode": "package", "version_id": 9}, 1
    )
    assert "已停用" in notice


def _page(blocks, **extra):
    return PageContent.model_validate(
        {
            "slide_id": "s1",
            "title": "落地路径",
            "source_refs": ["outline"],
            "blocks": [
                {
                    "id": f"b{i}",
                    "heading": f"阶段{i}",
                    "body": f"说明{i}。补充{i}。",
                    "source_refs": ["outline"],
                }
                for i in range(blocks)
            ],
            **extra,
        }
    )


def test_reference_lists_every_layout_and_requires_layout_field():
    package = load_builtin_package()
    text = build_outline_reference(package)
    for component in package.components:
        assert f'layout="{component.id}"' in text
    assert '必须增加字段 "layout"' in text


def test_outline_layout_key_survives_normalization():
    from landppt.services.outline.project_outline_normalization_service import (
        ProjectOutlineNormalizationService,
    )

    service = ProjectOutlineNormalizationService.__new__(ProjectOutlineNormalizationService)
    outline = service._standardize_outline_format(
        {
            "title": "t",
            "slides": [
                {"title": "封面", "slide_type": "title", "layout": "cover"},
                {"title": "要点", "content_points": ["a", "b", "c"], "layout": "points_3"},
            ],
        }
    )
    assert [s.get("layout") for s in outline["slides"]] == ["cover", "points_3"]

