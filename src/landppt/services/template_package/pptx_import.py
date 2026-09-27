"""PPTX -> fixed raster layers and confirmed, editable SVG content slots."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import uuid
from collections import Counter
from io import BytesIO
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

import fitz
from lxml import etree
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pydantic import Field

from .pptx_render import render_pdf
from .schemas import (
    ContentBlock,
    ContractModel,
    ItemRange,
    PackageAsset,
    PageComponent,
    PageContent,
    Slot,
    TemplatePackage,
    VisualBrief,
)
from .service import validate_package

MAX_UPLOAD = 50 * 1024 * 1024
SVG = "http://www.w3.org/2000/svg"

#: Smallest size the renderer keeps (see renderer: max(18, 75% of original)).
READABLE_SIZE = 18
LINE_HEIGHT = 1.4
#: Lowest point of a grown text box; below this sit footers and page numbers.
SAFE_BOTTOM = 680
#: Slots holding fewer characters than this cannot carry generated copy.
MIN_CAPACITY = {"title": 6, "subtitle": 8, "heading": 2, "body": 8}
MAX_GROWN_LINES = 4

# Ordinals, page numbers and part markers are design, not replaceable content.
_ORDINAL = re.compile(
    r"^(?:\d{1,3}|[一二三四五六七八九十]{1,3}|第?[一二三四五六七八九十\d]+[章节部分页篇]"
    r"|(?:part|chapter|no\.?|step)\s*[\w一二三四五六七八九十]{0,4}|\d{1,3}\s*/\s*\d{1,3})"
    r"[.、:：)）]?$",
    re.IGNORECASE,
)
_LATIN_TAG = re.compile(r"^[A-Z0-9 &/.,:'’\-|]{2,32}$")


def text_capacity(width, height, size):
    """Characters a box shows at the smallest size the renderer accepts."""
    size = max(READABLE_SIZE, size * 0.75)
    per_line = int(width // size)
    lines = int(max(0.0, height - size) // (size * LINE_HEIGHT)) + 1
    return max(0, per_line * lines)


def suggest_role(text, capacity):
    compact = " ".join(text.split())
    if _ORDINAL.match(compact) or _LATIN_TAG.match(compact):
        return "fixed"
    if capacity < MIN_CAPACITY["heading"] * 2:
        return "fixed"
    return "body"


def _overlap(a, b):
    """Horizontal overlap as a share of the narrower box."""
    left, right = max(a[0], b[0]), min(a[0] + a[2], b[0] + b[2])
    return max(0.0, right - left) / max(1.0, min(a[2], b[2]))


def grow_text_boxes(candidates, obstacles=()):
    """Source boxes hug the sample copy; extend them into the free space below.

    PowerPoint text boxes are usually auto-sized to their sample text, so their
    capacity says nothing about the space the designer left. A box grows down to
    the next element in its column, never past the footer line, and by at most
    a few lines, so decoration in the raster layer keeps its breathing room.
    """
    for c in candidates:
        if c["kind"] != "text":
            continue
        x, y, w, h = c["box"]
        size = c["font_size"]
        limit = min(SAFE_BOTTOM, 720) if y + h <= SAFE_BOTTOM else 720
        own_shape = c["id"].split(":")[0]
        others = [o["box"] for o in candidates if o is not c] + [
            box for sid, box, visible in obstacles if sid != own_shape or visible
        ]
        for ox, oy, ow, oh in others:
            if _overlap(c["box"], [ox, oy, ow, oh]) <= 0.2:
                continue
            if oy >= y + h - 1:
                limit = min(limit, oy - 4)
            elif oy <= y and oy + oh > y + h and ox <= x + 1 and ox + ow >= x + w - 1:
                # A card or panel around the text: stay inside its lower edge.
                limit = min(limit, oy + oh - 8)
        target = min(limit - y, size + (MAX_GROWN_LINES - 1) * size * LINE_HEIGHT)
        if target > h:
            c["box"] = [x, y, w, target]
        c["capacity"] = text_capacity(w, c["box"][3], size)


class ImportPage(ContractModel):
    slide: int = Field(ge=1, le=100)
    family: str = "points"
    bindings: dict[str, str]  # candidate ID -> title/subtitle/body/image/fixed
    groups: dict[str, str] = Field(default_factory=dict)


class ImportOptions(ContractModel):
    name: str = Field(min_length=1, max_length=255)
    pages: tuple[ImportPage, ...] = Field(min_length=1, max_length=40)


def normalize_pptx(data):
    """Validate before parsing; POTX/PPSX share OOXML but use another main type."""
    if not data or len(data) > MAX_UPLOAD:
        raise ValueError("PPTX 文件为空或超过 50 MB")
    output = BytesIO()
    try:
        with (
            ZipFile(BytesIO(data)) as source,
            ZipFile(output, "w", ZIP_DEFLATED) as target,
        ):
            entries = source.infolist()
            names = [e.filename for e in entries]
            if (
                len(entries) > 5000
                or len(names) != len(set(names))
                or sum(e.file_size for e in entries) > 200 * 1024 * 1024
            ):
                raise ValueError("PPTX 解压大小、文件数量或重复条目不符合限制")
            if "ppt/presentation.xml" not in names:
                raise ValueError("请选择 PPTX、POTX 或 PPSX；旧版 PPT 请先另存为 PPTX")
            for entry in entries:
                name = entry.filename
                if name.startswith("/") or ".." in name.split("/") or "\\" in name:
                    raise ValueError("PPTX 包含无效路径")
                # Embeddings are not necessarily executable: Excel, equations and
                # other ordinary OLE objects commonly use oleObject*.bin. Keep
                # them for the isolated renderer; only their pixels enter a pack.
                if "vbaproject" in name.lower():
                    raise ValueError(
                        f"检测到 VBA 宏组件：{name}。请另存为不含宏的 PPTX 后导入；"
                        "普通嵌入式表格和公式可以保留。"
                    )
                raw = source.read(entry)
                if name.endswith((".xml", ".rels")):
                    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
                        raise ValueError("PPTX 不允许 XML 外部实体")
                    root = etree.fromstring(
                        raw, etree.XMLParser(resolve_entities=False, no_network=True)
                    )
                    if root.getroottree().docinfo.doctype:
                        raise ValueError("PPTX 不允许 XML 文档类型声明")
                    if name.endswith(".rels"):
                        for rel in root:
                            if rel.get("TargetMode") == "External" and not rel.get(
                                "Type", ""
                            ).endswith("/hyperlink"):
                                raise ValueError(
                                    "请将外链图片、音视频转为内嵌资源后导入"
                                )
                    if name == "[Content_Types].xml":
                        for node in root:
                            if node.get("PartName") == "/ppt/presentation.xml":
                                node.set(
                                    "ContentType",
                                    "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
                                )
                        raw = etree.tostring(root)
                target.writestr(name, raw)
        prs = Presentation(BytesIO(output.getvalue()))
        if not 1 <= len(prs.slides) <= 100:
            raise ValueError("每次可分析 1–100 页，请拆分更大的文件")
        if not prs.slide_width or not prs.slide_height:
            raise ValueError("无效的页面尺寸")
        return output.getvalue()
    except (BadZipFile, etree.XMLSyntaxError, KeyError) as exc:
        raise ValueError("PPTX 文件无效") from exc


def raster_asset(raw):
    return PackageAsset(
        id=hashlib.sha256(raw).hexdigest(), data=base64.b64encode(raw).decode("ascii")
    )


def page_png(page, width=2560):
    scale = min(width / page.rect.width, width * 9 / 16 / page.rect.height)
    return page.get_pixmap(
        matrix=fitz.Matrix(scale, scale),
        alpha=False,
    ).tobytes("png")


def _fit(prs):
    scale = min(1280 / prs.slide_width, 720 / prs.slide_height)
    return (
        scale,
        (1280 - prs.slide_width * scale) / 2,
        (720 - prs.slide_height * scale) / 2,
    )


def _fallback_text_span(shape, paragraph, paragraphs, pdf_scale, scale):
    """Estimate a PDF-space span from authored text when extraction misses it."""
    runs = sorted(paragraph.runs, key=lambda run: len(run.text.strip()), reverse=True)
    fonts = [run.font for run in runs if run.text.strip()] + [paragraph.font]

    def authored(attribute, default):
        return next(
            (
                value
                for font in fonts
                if (value := getattr(font, attribute)) is not None
            ),
            default,
        )

    size = authored("size", READABLE_SIZE / scale) / pdf_scale
    color = 0x182F36
    for font in fonts:
        try:
            rgb = font.color.rgb
        except (AttributeError, TypeError, ValueError):
            continue  # Theme/inherited colors may have no direct RGB value.
        if rgb is not None:
            color = int(str(rgb), 16)
            break
    frame = shape.text_frame
    left = (shape.left + frame.margin_left) / pdf_scale
    top = (shape.top + frame.margin_top) / pdf_scale
    width = max(1, (shape.width - frame.margin_left - frame.margin_right) / pdf_scale)
    height = max(1, (shape.height - frame.margin_top - frame.margin_bottom) / pdf_scale)
    index = next(i for i, (_, item) in enumerate(paragraphs) if item is paragraph)
    top += height * index / len(paragraphs)
    return {
        "text": paragraph.text.strip(),
        "size": size,
        "font": authored("name", "Noto Sans CJK SC"),
        "flags": 16 if authored("bold", False) else 0,
        "color": color,
        "bbox": (left, top, left + width, top + max(size, height / len(paragraphs))),
        "origin": (left, top + size),
    }


def _candidates(prs, slide, page):
    scale, dx, dy = _fit(prs)
    pdf_scale = prs.slide_width / page.rect.width
    spans = [
        span
        for b in page.get_text("dict")["blocks"]
        if b.get("type") == 0
        for line in b["lines"]
        for span in line["spans"]
    ]
    candidates, warnings = [], []
    for shape in slide.shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.EMBEDDED_OLE_OBJECT:
            warnings.append(f"嵌入对象“{shape.name}”保留为固定底图，不转换为可编辑槽位")
            continue
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            warnings.append(f"组合对象“{shape.name}”保留为固定底图")
            continue
        if shape.rotation:
            warnings.append(f"旋转对象“{shape.name}”保留为固定底图")
            continue
        box = [
            dx + shape.left * scale,
            dy + shape.top * scale,
            shape.width * scale,
            shape.height * scale,
        ]
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            geometry = shape._element.xpath(".//a:prstGeom")
            preset = geometry[0].get("prst") if geometry else "rect"
            crop = {"ellipse": "circle", "roundRect": "rounded"}.get(preset, "rect")
            candidates.append(
                {
                    "id": str(shape.shape_id),
                    "kind": "image",
                    "text": shape.name,
                    "box": box,
                    "suggested": "fixed",
                    "crop": crop,
                }
            )
            continue
        if not shape.has_text_frame:
            continue
        frame = shape.text_frame
        paragraphs = [(i, p) for i, p in enumerate(frame.paragraphs) if p.text.strip()]
        for pindex, paragraph in paragraphs:
            text = paragraph.text.strip()
            local_spans = [
                s
                for s in spans
                if shape.left
                <= (s["bbox"][0] + s["bbox"][2]) / 2 * pdf_scale
                <= shape.left + shape.width
                and shape.top
                <= (s["bbox"][1] + s["bbox"][3]) / 2 * pdf_scale
                <= shape.top + shape.height
            ]
            matches = [
                s
                for s in local_spans
                if s["text"].strip()
                and (s["text"].strip() in text or text in s["text"].strip())
            ]
            fallback = not matches
            if fallback:
                matches = [
                    _fallback_text_span(shape, paragraph, paragraphs, pdf_scale, scale)
                ]
                warnings.append(
                    f"“{text[:24]}”未匹配到 PDF 文字样式，已使用 PPT 原有样式或默认样式继续解析"
                )
            main = max(matches, key=lambda s: len(s["text"]))
            font_size = main["size"] * pdf_scale * scale
            source_size = font_size
            issues = []
            if not math.isfinite(font_size) or font_size <= 0 or font_size > 160:
                issues.append(
                    "原字号超出槽位范围，需 AI 判断用途；可编辑槽位先采用合法字号"
                )
                font_size = (
                    min(160.0, font_size)
                    if math.isfinite(font_size) and font_size > 0
                    else float(READABLE_SIZE)
                )
            if len(text) > 2000:
                issues.append(
                    "原文超过单槽位 2000 字上限，需检查用途或拆页；不得截断正文"
                )
            warnings.extend(f"“{text[:24]}”：{issue}" for issue in issues)
            if font_size < READABLE_SIZE:
                # Generated copy must stay readable; small source text is enlarged
                # instead of being frozen into the background with its sample words.
                warnings.append(
                    f"“{text[:24]}”原字号 {font_size:.0f}px，作为槽位时放大到 {READABLE_SIZE}px"
                )
                font_size = float(READABLE_SIZE)
            x = box[0] + frame.margin_left * scale
            y = box[1] + frame.margin_top * scale
            w = box[2] - (frame.margin_left + frame.margin_right) * scale
            h = box[3] - (frame.margin_top + frame.margin_bottom) * scale
            if len(paragraphs) > 1:
                top = (
                    min(s["origin"][1] for s in matches) * pdf_scale * scale
                    + dy
                    - font_size
                )
                bottom = (
                    max(s["origin"][1] for s in matches) * pdf_scale * scale
                    + dy
                    + font_size * 0.3
                )
                y, h = top, max(font_size * 1.3, bottom - top)
            y = max(0, y)
            h = min(720 - y, max(h, font_size * 1.3))
            w = min(1280 - x, w)
            if x < 0 or w <= 0 or h < font_size:
                warnings.append(f"“{text[:24]}”文字框越界，保留固定内容")
                continue
            text_width = (
                sum(s["bbox"][2] - s["bbox"][0] for s in matches) * pdf_scale * scale
            )
            left = min(s["bbox"][0] for s in matches) * pdf_scale * scale + dx
            align = "left"
            if paragraph.alignment is not None:
                align = {2: "center", 3: "right"}.get(int(paragraph.alignment), "left")
            elif not fallback and abs(left - x - (w - text_width) / 2) < 5:
                align = "center"
            elif not fallback and abs(left - x - (w - text_width)) < 5:
                align = "right"
            candidates.append(
                {
                    "id": f"{shape.shape_id}:{pindex}",
                    "kind": "text",
                    "text": text,
                    "box": [x, y, w, h],
                    "font_size": font_size,
                    "font": (
                        main["font"]
                        if fallback
                        else paragraph.font.name
                        or next(
                            (r.font.name for r in paragraph.runs if r.font.name),
                            main["font"],
                        )
                    ),
                    "color": f"#{main['color']:06x}",
                    "bold": bool(main["flags"] & 16),
                    "align": align,
                    "valign": {3: "middle", 4: "bottom"}.get(
                        int(frame.vertical_anchor or 1), "top"
                    ),
                    "suggested": "body",
                    "source_size": source_size if math.isfinite(source_size) else None,
                    "repair_issues": issues,
                    "style_source": "pptx-default" if fallback else "pdf",
                }
            )
    obstacles = []
    for shape in slide.shapes:
        if shape.width and shape.height and not shape.rotation:
            # A filled or outlined text shape is a visible card: text stays inside.
            try:
                visible = shape.fill.type not in (None, 5) or bool(
                    shape.line.fill.type not in (None, 5)
                )
            except (AttributeError, NotImplementedError, TypeError, ValueError):
                visible = False
            obstacles.append(
                (
                    str(shape.shape_id),
                    [
                        dx + shape.left * scale,
                        dy + shape.top * scale,
                        shape.width * scale,
                        shape.height * scale,
                    ],
                    visible,
                )
            )
    grow_text_boxes(candidates, obstacles)
    for c in candidates:
        if c["kind"] == "text":
            c["suggested"] = suggest_role(c["text"], c["capacity"])
            # Size is not semantic: small card descriptions are still body copy.
            if c["box"][1] >= 720 * 0.9:
                c["suggested"] = "fixed"
    texts = [c for c in candidates if c["kind"] == "text"]
    if texts:
        title_shape = slide.shapes.title
        title = next(
            (
                c
                for c in texts
                if title_shape is not None
                and c["id"].split(":")[0] == str(title_shape.shape_id)
            ),
            None,
        )
        # Big ordinals such as "01" are decoration, not the page title.
        pool = [c for c in texts if c["suggested"] != "fixed"] or texts
        title = title or max(pool, key=lambda c: c["font_size"] - c["box"][1] / 100)
        title["suggested"] = "title"
        # A wide, smaller line right under the title reads as its subtitle.
        tx, ty, tw, th = title["box"]
        under = [
            c
            for c in texts
            if c is not title
            and c["suggested"] == "body"
            and 0
            <= c["box"][1] - (ty + title["font_size"] * LINE_HEIGHT)
            <= title["font_size"] * 2
            and c["font_size"] < title["font_size"]
            and _overlap(title["box"], c["box"]) >= 0.5
        ]
        if under:
            min(under, key=lambda c: c["box"][1])["suggested"] = "subtitle"
    return candidates, warnings


def _analyze(data, progress=None):
    if progress:
        progress("parse", "正在解析演示文件")
    normalized = normalize_pptx(data)
    prs = Presentation(BytesIO(normalized))
    if progress:
        progress("render", f"正在渲染原稿，共 {len(prs.slides)} 页")
    pdf, fonts = render_pdf(normalized)
    doc = fitz.open(stream=pdf, filetype="pdf")
    if len(doc) != len(prs.slides):
        doc.close()
        raise ValueError(
            "渲染页数与原稿不一致（可能含隐藏页），已停止导入以避免页码错位"
        )
    return normalized, prs, doc, fonts


def analyze_pptx(data, progress=None):
    _, prs, doc, fonts = _analyze(data, progress)
    warnings, slides = [], []
    if abs(prs.slide_width / prs.slide_height - 16 / 9) > 0.01:
        warnings.append("原稿不是 16:9，将等比居中放入 1280×720 画布并留白")
    # Check authored font names, not PDF names, which may already be substituted.
    authored = {
        n.get("typeface")
        for part in prs.part.package.iter_parts()
        if hasattr(part, "_element")
        for n in part._element.iter()
        if n.get("typeface") and not n.get("typeface").startswith("+")
    }
    installed = {f.strip().casefold() for family in fonts for f in family.split(",")}
    missing = sorted(f for f in authored if f.casefold() not in installed)
    if missing:
        warnings.append("渲染环境可能替换这些字体：" + "、".join(missing))
    try:
        for index, slide in enumerate(prs.slides):
            if progress:
                progress(
                    "extract", f"正在提取第 {index + 1}/{len(prs.slides)} 页文字和坐标"
                )
            candidates, problems = _candidates(prs, slide, doc[index])
            slides.append(
                {
                    "slide": index + 1,
                    "hidden": slide._element.get("show") == "0",
                    "preview": "data:image/png;base64,"
                    + base64.b64encode(page_png(doc[index], 640)).decode(),
                    "candidates": candidates,
                    "warnings": problems,
                }
            )
    finally:
        doc.close()
    return {"slides": slides, "warnings": warnings, "max_selected": 40}


def import_pptx(data, options, progress=None, repair=None):
    _, prs, original, _ = _analyze(data, progress)
    try:
        return _build_package(prs, original, options, progress, repair)
    finally:
        original.close()


def _build_package(prs, original, options, progress=None, repair=None):
    if len({p.slide for p in options.pages}) != len(options.pages):
        raise ValueError("不能重复选择同一页")
    assets, plans, failures, warnings = {}, [], [], []
    for choice in options.pages:
        if progress:
            progress("bind", f"正在绑定原稿第 {choice.slide} 页的内容槽位")
        if choice.slide > len(prs.slides):
            raise ValueError("选择的页码不存在")
        slide = prs.slides[choice.slide - 1]
        candidates, notes = _candidates(prs, slide, original[choice.slide - 1])
        lookup = {c["id"]: c for c in candidates}
        selected = [
            (lookup[key], role)
            for key, role in choice.bindings.items()
            if key in lookup and role not in {"fixed", "remove"}
        ]
        if set(choice.bindings) - lookup.keys():
            raise ValueError(f"第 {choice.slide} 页包含未知槽位")
        roles = Counter(role for _, role in selected)
        if (
            roles["title"] != 1
            or roles["subtitle"] > 1
            or roles["body"] + roles["heading"] > 40
            or roles["image"] > 8
        ):
            raise ValueError(
                f"第 {choice.slide} 页需要一个标题，至多一个副标题、40 个正文框（20 个要点）和 8 张配图"
            )
        for candidate, role in selected:
            if role not in {"title", "subtitle", "heading", "body", "image"} or (
                role == "image"
            ) != (candidate["kind"] == "image"):
                raise ValueError("槽位类型与内容不一致")
            shape_id = int(candidate["id"].split(":")[0])
            shape = next(s for s in slide.shapes if s.shape_id == shape_id)
            if role == "image":
                # Extract a PNG sample without depending on image-library lifetimes.
                from PIL import Image

                with Image.open(BytesIO(shape.image.blob)) as image:
                    out = BytesIO()
                    image.convert("RGBA").save(out, format="PNG")
                sample = raster_asset(out.getvalue())
                assets[sample.id] = sample
                candidate["sample"] = sample.id
                shape._element.getparent().remove(shape._element)
                warnings.append(
                    f"第 {choice.slide} 页的配图位于底图之上，请检查原稿中的遮罩和边框"
                )
            else:
                shape.text_frame.paragraphs[int(candidate["id"].split(":")[1])].clear()
        for key, role in choice.bindings.items():
            if role != "remove":
                continue
            candidate = lookup[key]
            shape_id, _, paragraph = key.partition(":")
            shape = next(s for s in slide.shapes if s.shape_id == int(shape_id))
            if candidate["kind"] == "text":
                shape.text_frame.paragraphs[int(paragraph)].clear()
            else:
                shape._element.getparent().remove(shape._element)
        # A typed-in page number of the source page would repeat on every page.
        for candidate in candidates:
            if (
                candidate["kind"] == "text"
                and candidate["box"][1] > 600
                and re.fullmatch(r"\d{1,3}(?:\s*/\s*\d{1,3})?", candidate["text"])
                and choice.bindings.get(candidate["id"], "fixed") == "fixed"
            ):
                shape_id, paragraph = candidate["id"].split(":")
                shape = next(s for s in slide.shapes if s.shape_id == int(shape_id))
                shape.text_frame.paragraphs[int(paragraph)].clear()
        # Remove system fields from slide, layout and master, including inherited fields.
        for container in (slide, slide.slide_layout, slide.slide_layout.slide_master):
            for field in list(container._element.xpath(".//a:fld")):
                if field.get("type", "").startswith(("slidenum", "datetime")):
                    field.getparent().remove(field)
        reference = raster_asset(page_png(original[choice.slide - 1]))
        assets[reference.id] = reference
        fixed_texts = [
            c["text"]
            for c in candidates
            if c["kind"] == "text" and choice.bindings.get(c["id"], "fixed") == "fixed"
        ]
        plans.append((choice, selected, reference, fixed_texts))
        warnings.extend(f"第 {choice.slide} 页：{n}" for n in notes)
    # Render only selected pages, in explicit order; hidden slides are made visible.
    chosen = [prs.slides[p.slide - 1].slide_id for p in options.pages]
    slide_ids = prs.slides._sldIdLst
    by_id = {int(n.get("id")): n for n in slide_ids}
    for n in list(slide_ids):
        slide_ids.remove(n)
    for sid in chosen:
        slide_ids.append(by_id[sid])
    for slide in prs.slides:
        slide._element.set("show", "1")
    output = BytesIO()
    prs.save(output)
    if progress:
        progress("background", f"正在渲染 {len(plans)} 页清除示例文字后的底图")
    background_pdf, _ = render_pdf(output.getvalue())
    components, fingerprints = [], set()
    with fitz.open(stream=background_pdf, filetype="pdf") as backgrounds:
        if len(backgrounds) != len(plans):
            raise ValueError("底图页数不匹配，导入已停止")
        for index, (choice, selected, reference, fixed_texts) in enumerate(plans):
            try:
                if progress:
                    progress(
                        "validate",
                        f"正在校验第 {index + 1}/{len(plans)} 个版式（原稿第 {choice.slide} 页）",
                    )
                audit_background_text(backgrounds[index], fixed_texts)
                background = raster_asset(page_png(backgrounds[index]))
                assets[background.id] = background
                component = _component(
                    prs, choice, selected, reference.id, background.id
                )
                repair_error = None
                for attempt in range(3):
                    try:
                        if attempt:
                            if progress:
                                progress(
                                    "repair",
                                    f"第 {choice.slide} 页版式校验失败，AI 正在第 {attempt}/2 次修复槽位",
                                )
                            component = repair(component, repair_error)
                        check_capacity(component, selected)
                        checked = _capacity_examples(component)
                        validate_package(
                            TemplatePackage(
                                contract_version=2,
                                package_id="import-check",
                                version=1,
                                name=options.name,
                                components=(checked,),
                                assets=tuple(assets.values()),
                            )
                        )
                        component = checked
                        if attempt:
                            warnings.append(
                                f"第 {choice.slide} 页已由 AI 修复槽位并通过校验，请对照原稿检查布局"
                            )
                        break
                    except ValueError as exc:
                        if repair is None or attempt == 2:
                            raise
                        repair_error = str(exc)
                signature = hashlib.sha256(
                    json.dumps(
                        {
                            "svg": component.svg,
                            "slots": [s.model_dump() for s in component.slots],
                        },
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                if signature in fingerprints:
                    warnings.append(f"第 {choice.slide} 页与已有版式相同，已合并")
                    continue
                components.append(component)
                fingerprints.add(signature)
            except ValueError as exc:
                failures.append({"slide": choice.slide, "error": str(exc)})
    if not components:
        raise ValueError(
            "没有版式通过校验："
            + "；".join(f"第 {f['slide']} 页：{f['error']}" for f in failures)
        )
    used = {c.reference_asset for c in components}
    for component in components:
        used.update(component.sample_assets.values())
        used.update(
            n.get("data-asset")
            for n in etree.fromstring(component.svg.encode()).iter()
            if n.get("data-asset")
        )
    package = TemplatePackage(
        contract_version=2,
        package_id="pptx_" + uuid.uuid4().hex,
        version=1,
        name=options.name,
        description="从本地 PPTX 导入；固定底图 + 可编辑内容槽位",
        components=tuple(components),
        assets=tuple(a for key, a in assets.items() if key in used),
    )
    return package, {
        "failures": failures,
        "warnings": warnings,
        "partial": bool(failures),
    }


def audit_background_text(page, fixed_texts):
    """Unrecognized/grouped/inherited copy must not silently become a backdrop."""
    permitted = [re.sub(r"\s+", "", text) for text in fixed_texts]
    leftover = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"])
            compact = re.sub(r"\s+", "", text)
            if compact and not any(compact in allowed for allowed in permitted):
                leftover.append(text[:40])
    if leftover:
        raise ValueError(
            "底图仍含未确认的原稿文字："
            + "；".join(leftover[:5])
            + "。请将组合、图表或母版中的这些文字转换为普通文本框后重新分析"
        )


def check_capacity(component, selected):
    """A slot too small for generated copy makes every page on it fail later."""
    from ..slide.package_generation.capacity import slot_limits

    limits = slot_limits(component)
    by_node = {slot.node_id: slot.field for slot in component.slots}
    texts = {f"slot-{i}": c["text"] for i, (c, _) in enumerate(selected)}
    problems = []
    for node_id, field in by_node.items():
        if field not in limits:
            continue
        kind = field.rsplit(".", 1)[-1] if field.startswith("blocks[") else field
        need = MIN_CAPACITY.get(kind, 0)
        if limits[field] < need:
            problems.append(
                f"“{texts.get(node_id, '')[:12]}”只能容纳 {limits[field]} 字"
                f"（至少需要 {need} 字）"
            )
    if problems:
        raise ValueError(
            "；".join(problems)
            + "。请在原稿中扩大文字区域或拆页后重新分析；只有跨主题复用的装饰文字才适合固定在底图，正文不应固定"
        )


def _capacity_examples(component):
    """Keep the source copy and exercise shorter and longer replacement text."""
    from ..slide.package_generation.capacity import slot_limits

    budgets = slot_limits(component)
    examples = [component.examples[0]]
    for label, longer in (("short", False), ("long", True)):
        payload = component.examples[0].model_dump(mode="json")
        payload["slide_id"] = label
        for slot in component.slots:
            if slot.kind != "text":
                continue
            length = min(80 if longer else 4, max(1, int(budgets[slot.field] * 0.65)))
            sample = ("示例内容" * 21)[:length]
            if slot.field.startswith("blocks["):
                index = int(slot.field.split("[")[1].split("]")[0])
                payload["blocks"][index][slot.field.rsplit(".", 1)[1]] = sample
            else:
                payload[slot.field] = sample
        examples.append(PageContent.model_validate(payload))
    return component.model_copy(update={"examples": tuple(examples)})


def pair_blocks(bodies):
    """Group body candidates into blocks: a heading directly above its text.

    Imported cards usually stack a short label over a sentence; binding each as
    its own block doubles the point count and leaves one-line bodies nobody can
    write for. Reading order is top-to-bottom within columns, then left-to-right.
    """
    remaining = sorted(bodies, key=lambda c: (round(c["box"][1] / 24), c["box"][0]))
    used, blocks = set(), []
    for index, top in enumerate(remaining):
        if index in used:
            continue
        below = None
        for other_index, other in enumerate(remaining):
            if other_index in used or other_index == index:
                continue
            gap = other["box"][1] - (top["box"][1] + top["font_size"] * LINE_HEIGHT)
            if (
                -2 <= gap <= max(top["font_size"], other["font_size"]) * 1.6
                and _overlap(top["box"], other["box"]) >= 0.5
                and (
                    top["font_size"] > other["font_size"] + 0.5
                    or (top["bold"] and not other["bold"])
                )
                and len(top["text"]) <= len(other["text"])
                and (below is None or other["box"][1] < below[1]["box"][1])
            ):
                below = (other_index, other)
        used.add(index)
        if below:
            used.add(below[0])
            blocks.append((top, below[1]))
        else:
            blocks.append((None, top))
    return blocks


def _component(prs, choice, selected, reference_id, background_id):
    scale, dx, dy = _fit(prs)
    root = etree.Element(f"{{{SVG}}}svg", nsmap={None: SVG}, viewBox="0 0 1280 720")
    etree.SubElement(
        root, f"{{{SVG}}}rect", x="0", y="0", width="1280", height="720", fill="#ffffff"
    )
    etree.SubElement(
        root,
        f"{{{SVG}}}image",
        {
            "id": "background",
            "x": str(dx),
            "y": str(dy),
            "width": str(prs.slide_width * scale),
            "height": str(prs.slide_height * scale),
            "data-asset": background_id,
            "data-role": "background",
        },
    )
    slots, blocks, visuals, samples = [], [], [], {}
    title, subtitle = "", ""
    bodies = [c for c, role in selected if role == "body"]
    if choice.groups or any(role == "heading" for _, role in selected):
        from .pptx_analysis import normalize_point_groups

        texts = [(c, role) for c, role in selected if role in {"body", "heading"}]
        points = normalize_point_groups(
            [
                (c["id"], role, choice.groups.get(c["id"], c["id"]), c["box"])
                for c, role in texts
            ]
        )
        grouped = {}
        for c, _ in texts:
            role, group = points[c["id"]]
            grouped.setdefault(group, {})[role] = c
        selected = [
            (c, points[c["id"]][0] if c["id"] in points else role)
            for c, role in selected
        ]
        pairs = [(row.get("heading"), row["body"]) for row in grouped.values()]
    else:
        pairs = pair_blocks(bodies)
    fields = {}
    for heading, body in pairs:
        number = len(blocks)
        blocks.append(
            ContentBlock(
                id=f"b{number}",
                heading=heading["text"] if heading else "",
                body=body["text"],
            )
        )
        if heading:
            fields[id(heading)] = f"blocks[{number}].heading"
        fields[id(body)] = f"blocks[{number}].body"
    for index, (c, role) in enumerate(selected):
        node_id = f"slot-{index}"
        x, y, w, h = c["box"]
        if role == "image":
            vid = f"image-{len(visuals)}"
            field = f"visual_briefs[{len(visuals)}].id"
            visuals.append(VisualBrief(id=vid, brief="替换为与页面内容相关的配图"))
            samples[vid] = c["sample"]
            etree.SubElement(
                root,
                f"{{{SVG}}}image",
                {
                    "id": node_id,
                    "x": str(x),
                    "y": str(y),
                    "width": str(w),
                    "height": str(h),
                    "data-crop": c.get("crop", "rect"),
                },
            )
        else:
            field = role
            if role == "title":
                title = c["text"]
            elif role == "subtitle":
                subtitle = c["text"]
            else:
                field = fields[id(c)]
            size = c["font_size"]
            etree.SubElement(
                root,
                f"{{{SVG}}}text",
                {
                    "id": node_id,
                    "x": str(x),
                    "y": str(y + size),
                    "font-size": str(size),
                    "font-family": f"{c['font']},Arial,'Microsoft YaHei','Noto Sans CJK SC',sans-serif",
                    "font-weight": "bold" if c["bold"] else "normal",
                    "fill": c["color"],
                    "data-box-w": str(w),
                    "data-box-h": str(h),
                    "data-align": c["align"],
                    "data-valign": c["valign"],
                    "text-anchor": "start",
                },
            )
        slots.append(
            Slot(
                node_id=node_id,
                field=field,
                kind="image" if role == "image" else "text",
                max_chars=500 if role == "title" else 2000,
            )
        )
    example = PageContent(
        slide_id="sample",
        title=title,
        subtitle=subtitle,
        blocks=tuple(blocks),
        visual_briefs=tuple(visuals),
    )
    return PageComponent(
        id=f"slide-{choice.slide}",
        family=choice.family,
        description=(
            f"原稿第 {choice.slide} 页：{len(blocks)} 个要点"
            + ("（小标题+说明）" if any(b.heading for b in blocks) else "")
            + (f"、{len(visuals)} 张配图" if visuals else "")
            + ("，含副标题" if subtitle else "")
        ),
        svg=etree.tostring(root, encoding="unicode"),
        slots=tuple(slots),
        blocks=ItemRange(minimum=len(blocks), maximum=len(blocks)),
        images=ItemRange(minimum=len(visuals), maximum=len(visuals)),
        examples=(example,),
        reference_asset=reference_id,
        source_slide=choice.slide,
        sample_assets=samples,
    )
