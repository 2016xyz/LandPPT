"""Validate an imported visual package and render a different subject.

Run inside the development container with --generate-copy to exercise the
configured content model. Without that flag this uses deterministic new copy.
No drafts or project pages are written to the database.
"""

import argparse
import asyncio
import base64
import json
from pathlib import Path

from playwright.async_api import async_playwright

from landppt.services.slide.package_generation.content_service import ContentService
from landppt.services.slide.package_generation.renderer import render_page
from landppt.services.slide.package_generation.slot_content import content_from_fields
from landppt.services.template_package.archive import export_archive, import_archive
from landppt.services.template_package.schemas import PageContent, TemplatePackage
from landppt.services.template_package.service import example_assets, validate_package


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    copy_source = parser.add_mutually_exclusive_group()
    copy_source.add_argument("--generate-copy", action="store_true")
    copy_source.add_argument(
        "--copy", type=Path, help="Reuse previously generated copy"
    )
    parser.add_argument("--user-id", type=int, default=1)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    package = TemplatePackage.model_validate_json(args.package.read_text("utf-8"))
    validate_package(package)
    round_trip = import_archive(export_archive(package))
    assert round_trip == package
    component = package.components[0]
    assert not component.images.maximum, "Choose a text layout for this copy check"
    slide = {
        "slide_id": "new-subject",
        "title": "社区环保行动方案",
        "layout": component.id,
        "points": [
            "行动方向包括垃圾分类、节约用水与低碳出行。",
            "垃圾分类：设置分类投放点，开展居民宣传。",
            "节约用水：检查公共设施漏水，倡导按需用水。",
            "低碳出行：鼓励步行、骑行及乘坐公共交通。",
            "实施步骤包括需求调研、方案制定、志愿培训与持续反馈。",
            "需求调研：收集居民意见，记录社区环境问题。",
            "方案制定：确定行动清单，明确负责人。",
            "志愿培训：讲解分类规则与服务流程。",
            "持续反馈：定期收集建议，调整后续行动。",
        ],
    }
    if args.copy:
        replacement = PageContent.model_validate_json(args.copy.read_text("utf-8"))
    elif args.generate_copy:
        from landppt.api.template_package_import_api import analysis_service

        valid, errors, _ = await ContentService(analysis_service(args.user_id)).expand(
            {"requirements": slide["title"], "outline": [slide]}, package, [slide]
        )
        assert not errors, errors
        replacement = valid[slide["slide_id"]]
    else:
        fields = {}
        for slot in component.slots:
            if slot.field == "title":
                fields[slot.field] = slide["title"]
            elif slot.field.endswith(".heading"):
                fields[slot.field] = "环保行动"
            else:
                fields[slot.field] = "共建美好社区"
        replacement = content_from_fields(
            {"slide_id": slide["slide_id"], "fields": fields, "source_refs": []},
            component,
        )
    assets = example_assets(package, component)
    sample = render_page(
        package,
        component.id,
        component.examples[0],
        assets=assets,
        allowed_image_urls=frozenset(assets.values()),
    )
    changed = render_page(package, component.id, replacement)
    for original_topic in ("Kubernetes", "Minikube", "kubectl", "Docker"):
        assert original_topic.casefold() not in changed.svg.casefold(), original_topic
    assert "data-asset" not in component.svg
    assert "data:image" not in changed.svg
    reference = next(a for a in package.assets if a.id == component.reference_asset)
    (args.output / "original.png").write_bytes(base64.b64decode(reference.data))
    (args.output / "replacement.json").write_text(
        replacement.model_dump_json(indent=2), encoding="utf-8"
    )
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 720})
        for name, rendered in (("sample", sample), ("replacement", changed)):
            (args.output / f"{name}.html").write_text(rendered.html_content, "utf-8")
            await page.set_content(rendered.html_content)
            await page.evaluate("document.fonts.ready")
            await page.screenshot(path=str(args.output / f"{name}.png"))
        await browser.close()
    report = {
        "component": component.id,
        "blocks": len(replacement.blocks),
        "slots": len(component.slots),
        "model_copy": args.generate_copy,
        "package_validation": "passed",
        "archive_round_trip": "passed",
        "replacement_render": "passed",
        "original_topic_absent": True,
    }
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
    )
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
