"""Raster-backed package contracts and PPTX conversion regression tests."""

import base64
import hashlib
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZIP_DEFLATED, ZipFile

import fitz
import pytest
from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

from landppt.services.slide.package_generation.renderer import render_page
from landppt.services.template_package import editor, pptx_import
from landppt.services.template_package.archive import export_archive, import_archive
from landppt.services.template_package.builtin import load_builtin_package
from landppt.services.template_package.schemas import PackageAsset
from landppt.services.template_package.service import validate_package


def png(color="white"):
    out = BytesIO()
    Image.new("RGB", (160, 90), color).save(out, format="PNG")
    return out.getvalue()


def package_with_background():
    base = load_builtin_package()
    asset = pptx_import.raster_asset(png())
    component = base.components[0]
    svg = component.svg.replace(
        ">",
        f'><image id="import-background" x="0" y="0" width="1280" height="720" data-role="background" data-asset="{asset.id}"/>',
        1,
    )
    component = component.model_copy(update={"svg": svg})
    return base.model_copy(
        update={"contract_version": 2, "assets": (asset,), "components": (component,)}
    )


def test_fixed_asset_render_and_zip_roundtrip():
    package = package_with_background()
    validate_package(package)
    rendered = render_page(
        package, package.components[0].id, package.components[0].examples[0]
    )
    assert package.assets[0].url() in rendered.svg
    restored = import_archive(export_archive(package))
    assert restored == package
    assert restored.content_hash() == package.content_hash()


def test_legacy_hash_is_unchanged():
    package = load_builtin_package()
    old = package.model_dump(mode="json")
    old.pop("assets")
    for component in old["components"]:
        for key in ("reference_asset", "source_slide", "sample_assets"):
            component.pop(key)
    digest = hashlib.sha256(
        json.dumps(
            old, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    assert package.content_hash() == digest


def test_asset_rejects_tampering_and_external_urls():
    asset = pptx_import.raster_asset(png())
    with pytest.raises(ValueError, match="hash mismatch"):
        PackageAsset.model_validate(
            {**asset.model_dump(), "data": base64.b64encode(png("red")).decode()}
        )
    package = package_with_background()
    c = package.components[0]
    with pytest.raises(ValueError, match="not href"):
        validate_package(
            package.model_copy(
                update={
                    "components": (
                        c.model_copy(
                            update={
                                "svg": c.svg.replace(
                                    "data-asset=",
                                    'href="https://example.invalid/x" data-asset=',
                                )
                            }
                        ),
                    )
                }
            )
        )
    with pytest.raises(ValueError, match="package asset"):
        validate_package(package.model_copy(update={"assets": ()}))


def test_archive_rejects_extra_path_and_corrupt_asset():
    raw = export_archive(package_with_background())
    out = BytesIO(raw)
    with ZipFile(out, "a", ZIP_DEFLATED) as archive:
        archive.writestr("../escape", b"x")
    with pytest.raises(ValueError, match="未声明"):
        import_archive(out.getvalue())


def fixture_pptx(count=3):
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333333), Inches(7.5)
    for index in range(count):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        if index == 1:
            slide._element.set("show", "0")
        title = slide.shapes.add_textbox(
            Inches(1), Inches(0.6), Inches(11.3), Inches(1)
        )
        p = title.text_frame.paragraphs[0]
        p.text = f"Slide {index+1} title"
        p.alignment = PP_ALIGN.CENTER
        p.font.size = Pt(32)
        p.font.name = "Liberation Sans"
        p.font.color.rgb = RGBColor(24, 47, 54)
        body = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(11.3), Inches(2))
        p = body.text_frame.paragraphs[0]
        p.text = f"Body content {index+1}"
        p.font.size = Pt(24)
        p.font.name = "Liberation Sans"
        p.font.color.rgb = RGBColor(24, 47, 54)
        footer = slide.shapes.add_textbox(
            Inches(1), Inches(6.8), Inches(11), Inches(0.3)
        )
        p = footer.text_frame.paragraphs[0]
        p.text = f"Fixed brand footer {index+1}"
        p.font.size = Pt(10)
        p.font.name = "Liberation Sans"
        photo = slide.shapes.add_picture(
            BytesIO(png("#C5D8DF")), Inches(8), Inches(4.3), Inches(3), Inches(1.6875)
        )
        photo._element.xpath(".//a:prstGeom")[0].set("prst", "ellipse")
    output = BytesIO()
    prs.save(output)
    return output.getvalue()


def fake_render(data):
    """PDF text positions mirror fixture geometry; catches page selection drift."""
    prs = Presentation(BytesIO(data))
    doc = fitz.open()
    for slide in prs.slides:
        page = doc.new_page(width=960, height=540)
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for paragraph in shape.text_frame.paragraphs:
                if paragraph.text:
                    size = paragraph.font.size.pt if paragraph.font.size else 24
                    page.insert_text(
                        (shape.left / 12700 + 7.2, shape.top / 12700 + size + 3.6),
                        paragraph.text,
                        fontsize=size,
                    )
    output = doc.tobytes()
    doc.close()
    return output, ["Liberation Sans"]


def test_analyze_select_hidden_and_nonconsecutive_pages(monkeypatch):
    monkeypatch.setattr(pptx_import, "render_pdf", fake_render)
    raw = fixture_pptx()
    result = pptx_import.analyze_pptx(raw)
    assert result["slides"][1]["hidden"]
    assert any("字号" in warning for warning in result["slides"][0]["warnings"])
    choices = [
        {
            "slide": s["slide"],
            "family": "points",
            "bindings": {c["id"]: c["suggested"] for c in s["candidates"]},
        }
        for s in (result["slides"][2], result["slides"][1])
    ]
    package, report = pptx_import.import_pptx(
        raw, pptx_import.ImportOptions(name="Imported", pages=choices)
    )
    assert not report["failures"]
    assert [c.source_slide for c in package.components] == [3, 2]
    assert package.components[0].examples[0].title == "Slide 3 title"
    validate_package(package)
    assert import_archive(export_archive(package)) == package


def test_rejects_page_count_mismatch(monkeypatch):
    monkeypatch.setattr(
        pptx_import, "render_pdf", lambda data: fake_render(fixture_pptx(1))
    )
    with pytest.raises(ValueError, match="页数"):
        pptx_import.analyze_pptx(fixture_pptx(3))


def test_normalizes_potx_and_rejects_external_relationships():
    data = fixture_pptx(1)
    out = BytesIO()
    with ZipFile(BytesIO(data)) as source, ZipFile(out, "w", ZIP_DEFLATED) as target:
        for name in source.namelist():
            raw = source.read(name)
            if name == "[Content_Types].xml":
                raw = raw.replace(
                    b"presentationml.presentation.main+xml",
                    b"presentationml.template.main+xml",
                )
            target.writestr(name, raw)
    normalized = pptx_import.normalize_pptx(out.getvalue())
    assert len(Presentation(BytesIO(normalized)).slides) == 1
    bad = BytesIO()
    with ZipFile(BytesIO(data)) as source, ZipFile(bad, "w", ZIP_DEFLATED) as target:
        for name in source.namelist():
            raw = source.read(name)
            if name == "ppt/slides/_rels/slide1.xml.rels":
                raw = raw.replace(
                    b"</Relationships>",
                    b'<Relationship Id="evil" Type="image" TargetMode="External" Target="file:///etc/passwd"/></Relationships>',
                )
            target.writestr(name, raw)
    with pytest.raises(ValueError, match="外链"):
        pptx_import.normalize_pptx(bad.getvalue())


@pytest.mark.parametrize("suffix", ["bin", "xls", "docx"])
def test_normalize_keeps_ordinary_embedded_files(suffix):
    data = BytesIO(fixture_pptx(1))
    path = f"ppt/embeddings/oleObject1.{suffix}"
    with ZipFile(data, "a", ZIP_DEFLATED) as archive:
        archive.writestr(path, b"embedded document fixture")
    normalized = pptx_import.normalize_pptx(data.getvalue())
    with ZipFile(BytesIO(normalized)) as archive:
        assert archive.read(path) == b"embedded document fixture"


def test_real_vba_component_has_distinct_error():
    data = BytesIO(fixture_pptx(1))
    with ZipFile(data, "a", ZIP_DEFLATED) as archive:
        archive.writestr("ppt/vbaProject.bin", b"macro fixture")
    with pytest.raises(ValueError, match="检测到 VBA 宏组件：ppt/vbaProject.bin"):
        pptx_import.normalize_pptx(data.getvalue())


def test_embedded_ole_reaches_renderer_as_fixed_content(monkeypatch):
    prs = Presentation(BytesIO(fixture_pptx(1)))
    ole = prs.slides[0].shapes.add_ole_object(
        BytesIO(b"embedded equation fixture"),
        "Equation.3",
        Inches(1),
        Inches(4),
        width=Inches(2),
        height=Inches(1),
        icon_file=BytesIO(png("blue")),
    )
    output = BytesIO()
    prs.save(output)
    rendered = []

    def render(data):
        with ZipFile(BytesIO(data)) as archive:
            assert any(
                name.startswith("ppt/embeddings/") and name.endswith(".bin")
                for name in archive.namelist()
            )
        rendered.append(data)
        return fake_render(data)

    monkeypatch.setattr(pptx_import, "render_pdf", render)
    analysis = pptx_import.analyze_pptx(output.getvalue())
    slide = analysis["slides"][0]
    assert any("嵌入对象" in warning for warning in slide["warnings"])
    assert not any(
        c["id"].split(":")[0] == str(ole.shape_id) for c in slide["candidates"]
    )
    settings = pptx_import.ImportOptions(
        name="OLE template",
        pages=[
            {
                "slide": 1,
                "bindings": {c["id"]: c["suggested"] for c in slide["candidates"]},
            }
        ],
    )
    package, report = pptx_import.import_pptx(output.getvalue(), settings)
    assert not report["failures"]
    assert len(rendered) == 3
    with ZipFile(BytesIO(export_archive(package))) as archive:
        assert all(
            name == "manifest.json" or name.startswith("assets/")
            for name in archive.namelist()
        )


def test_centered_text_is_measured_at_its_actual_position():
    package = load_builtin_package()
    c = package.components[0]
    c = c.model_copy(
        update={
            "svg": c.svg.replace(
                "data-box-w=", 'data-align="right" data-valign="middle" data-box-w='
            )
        }
    )
    package = package.model_copy(update={"components": (c,)})
    result = render_page(package, c.id, c.examples[0])
    assert "tspan" in result.svg
    assert 'data-align="right"' in result.svg


@pytest.mark.asyncio
async def test_ai_edit_cannot_drop_fixed_background():
    package = package_with_background()
    component = package.components[0]
    original = load_builtin_package().components[0]
    content = SimpleNamespace(
        json_completion=AsyncMock(return_value=({"changes": {"svg": original.svg}}, {}))
    )
    operation = editor.ComponentEdit(
        action="modify", component_id=component.id, instruction="修改标题"
    )
    with pytest.raises(ValueError, match="固定底图"):
        await editor.edit_component(content, package, operation, "修改标题", component)
    assert content.json_completion.await_count == 2


def test_crop_is_generated_by_renderer_not_taken_from_template():
    package = load_builtin_package()
    component = next(c for c in package.components if c.images.minimum)
    from landppt.services.slide.svg_page.sanitize import parse_svg, serialize

    root = parse_svg(component.svg)
    nodes = {n.get("id"): n for n in root.iter()}
    for slot in component.slots:
        if slot.kind == "image":
            nodes[slot.node_id].set("data-crop", "circle")
    component = component.model_copy(update={"svg": serialize(root)})
    package = package.model_copy(update={"components": (component,)})
    sample = pptx_import.raster_asset(png()).url()
    result = render_page(
        package,
        component.id,
        component.examples[0],
        assets={v.id: sample for v in component.examples[0].visual_briefs},
        allowed_image_urls=frozenset({sample}),
    )
    assert "<clipPath" in result.svg and "<ellipse" in result.svg
    for slot in component.slots:
        if slot.kind == "image":
            nodes[slot.node_id].set("clip-path", "url(#injected)")
    with pytest.raises(ValueError, match="direct, visible geometry"):
        validate_package(
            package.model_copy(
                update={
                    "components": (
                        component.model_copy(update={"svg": serialize(root)}),
                    )
                }
            )
        )


@pytest.mark.asyncio
async def test_upload_routes_require_auth_and_scope_saved_draft(monkeypatch):
    import httpx
    from fastapi import FastAPI

    from landppt.api import template_package_import_api as api

    app = FastAPI()
    app.include_router(api.router)
    saved = AsyncMock(return_value={"id": 42, "status": "draft"})
    users = []

    def catalog(uid):
        users.append(uid)
        return SimpleNamespace(create=saved)

    monkeypatch.setattr(api, "PackageCatalog", catalog)

    async def classified(result, service, progress, *, vision=False):
        return result

    monkeypatch.setattr(api, "classify_analysis", classified)
    monkeypatch.setattr(api, "analysis_service", lambda uid: None)
    monkeypatch.setattr(pptx_import, "render_pdf", fake_render)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        unauthorized = await client.post(
            "/api/global-master-templates/packages-pptx/analyze",
            files={"file": ("deck.pptx", fixture_pptx(1))},
        )
        assert unauthorized.status_code in {401, 403}
        assert not users
        app.dependency_overrides[api.get_current_user_required] = (
            lambda: SimpleNamespace(id=7)
        )
        raw = fixture_pptx(1)
        analysis = await client.post(
            "/api/global-master-templates/packages-pptx/analyze",
            files={"file": ("deck.pptx", raw)},
        )
        assert analysis.status_code == 200
        slide = analysis.json()["slides"][0]
        options = {
            "name": "Test",
            "pages": [
                {
                    "slide": 1,
                    "bindings": {c["id"]: c["suggested"] for c in slide["candidates"]},
                }
            ],
        }
        response = await client.post(
            "/api/global-master-templates/packages-pptx/import",
            files={"file": ("deck.pptx", raw)},
            data={"options": json.dumps(options)},
        )
        assert response.status_code == 200, response.text
        assert users == [7]
        assert response.json()["package"]["status"] == "draft"
        assert len(saved.call_args.args[0].components[0].examples) == 3


def _text(cid, text, box, size=20.0, bold=False):
    return {
        "id": cid,
        "kind": "text",
        "text": text,
        "box": list(box),
        "font_size": size,
        "bold": bold,
    }


def test_ordinals_page_numbers_and_tags_are_suggested_fixed():
    from landppt.services.template_package.pptx_import import suggest_role

    for text in ("01", "04", "3/12", "第二章", "PART 01", "GEOGRAPHY", "二"):
        assert suggest_role(text, 40) == "fixed", text
    assert suggest_role("防滑", 1) == "fixed"
    assert suggest_role("山路湿滑，建议穿防滑登山鞋", 60) == "body"


def test_sample_hugging_boxes_grow_into_free_space_but_not_past_neighbours():
    from landppt.services.template_package.pptx_import import grow_text_boxes

    heading = _text("1:0", "防滑", (100, 200, 300, 30), 24, True)
    body = _text("2:0", "山路湿滑", (100, 240, 300, 30), 20)
    below = _text("3:0", "保暖", (100, 400, 300, 30), 24, True)
    grow_text_boxes([heading, body, below])
    assert heading["box"][1] + heading["box"][3] <= 236
    assert body["box"][1] + body["box"][3] <= 396
    assert body["box"][3] > 30 and body["capacity"] >= 30


def test_visible_card_limits_growth():
    from landppt.services.template_package.pptx_import import grow_text_boxes

    body = _text("2:0", "说明", (110, 210, 280, 30), 20)
    grow_text_boxes([body], [("9", [100, 200, 300, 80], True)])
    assert body["box"][1] + body["box"][3] <= 272


def test_heading_is_paired_with_the_text_beneath_it():
    from landppt.services.template_package.pptx_import import pair_blocks

    h1 = _text("1:0", "防滑", (100, 200, 300, 30), 24, True)
    b1 = _text("2:0", "山路湿滑，建议穿防滑鞋", (100, 236, 300, 60), 18)
    h2 = _text("3:0", "保暖", (500, 200, 300, 30), 24, True)
    b2 = _text("4:0", "山顶温差大，需带外套", (500, 236, 300, 60), 18)
    lone = _text("5:0", "以上建议适用于全年", (100, 500, 700, 40), 18)
    blocks = pair_blocks([b2, lone, h1, b1, h2])
    assert [(h and h["text"], b["text"]) for h, b in blocks] == [
        ("防滑", "山路湿滑，建议穿防滑鞋"),
        ("保暖", "山顶温差大，需带外套"),
        (None, "以上建议适用于全年"),
    ]
