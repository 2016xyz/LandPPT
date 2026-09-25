"""Validate and render the built-in package without API keys or a database."""

from __future__ import annotations

import argparse
import base64
import html
import io
import json
import time
from pathlib import Path

from PIL import Image, ImageDraw

from landppt.services.slide.package_generation.renderer import render_page
from landppt.services.template_package.builtin import load_builtin_package
from landppt.services.template_package.service import validate_package


def sample_image_url() -> str:
    """A deterministic raster fixture, explicitly labelled as preview artwork."""
    canvas = Image.new("RGB", (960, 600), "#DFE7E1")
    draw = ImageDraw.Draw(canvas)
    draw.line([(160, 400), (480, 290), (800, 160)], fill="#182F36", width=8)
    for x, y in ((160, 400), (480, 290), (800, 160)):
        draw.ellipse((x - 42, y - 42, x + 42, y + 42), fill="#B7462D")
    draw.text((50, 40), "SAMPLE VISUAL / PREVIEW ONLY", fill="#182F36", font_size=28)
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode(
        "ascii"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/template-package")
    )
    args = parser.parse_args()
    package = load_builtin_package()
    report = validate_package(package)
    args.output.mkdir(parents=True, exist_ok=True)
    image_url = sample_image_url()
    samples = []
    links = []
    for component in package.components:
        content = component.examples[0]
        start = time.perf_counter()
        rendered = render_page(
            package,
            component.id,
            content,
            assets={visual.id: image_url for visual in content.visual_briefs},
            allowed_image_urls=frozenset({image_url}),
        )
        elapsed = time.perf_counter() - start
        filename = f"{component.id}.html"
        (args.output / filename).write_text(rendered.html_content, encoding="utf-8")
        samples.append(
            {
                "component": component.id,
                "render_seconds": elapsed,
                "metadata": rendered.metadata,
            }
        )
        links.append(
            f'<li><a href="{filename}">{html.escape(component.id)} — '
            f"{html.escape(component.description)}</a></li>"
        )
    report["samples"] = samples
    report["timing_scope"] = (
        "Local binding and layout only; excludes LLM, images, network and export."
    )
    (args.output / "manifest.json").write_text(
        package.model_dump_json(indent=2), encoding="utf-8"
    )
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output / "index.html").write_text(
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>模板包预览</title>'
        "<body><h1>清晰叙事 · 模板包预览</h1><p>8 类版式，10 个组件。指标与图片为演示素材。</p><ul>"
        + "".join(links)
        + "</ul></body></html>",
        encoding="utf-8",
    )
    print(
        f"Validated {report['components']} components; "
        f"preview: {args.output / 'index.html'}"
    )


if __name__ == "__main__":
    main()
