"""A built-in editorial SVG package with eight page families and process variants."""

from __future__ import annotations

from lxml import etree

from ..slide.svg_page.constants import SVG_NS
from ..slide.svg_page.sanitize import serialize
from .schemas import (
    ContentBlock,
    ItemRange,
    Metric,
    PackageTheme,
    PageComponent,
    PageContent,
    Slot,
    TemplatePackage,
    VisualBrief,
)


class _Page:
    def __init__(self, theme: PackageTheme):
        self.theme = theme
        self.root = etree.Element(
            f"{{{SVG_NS}}}svg",
            nsmap={None: SVG_NS},
            viewBox="0 0 1280 720",
            width="1280",
            height="720",
        )
        self.slots = []
        self.shape(
            "rect",
            x=0,
            y=0,
            width=1280,
            height=720,
            fill=theme.background,
            **{"data-role": "background"},
        )
        self.shape("rect", x=80, y=46, width=52, height=6, fill=theme.accent)
        self.shape(
            "line",
            x1=80,
            y1=612,
            x2=1200,
            y2=612,
            stroke=theme.foreground,
            **{"stroke-width": "1"},
        )
        self.text(
            "takeaway",
            80,
            658,
            1120,
            34,
            22,
            required=False,
            max_chars=90,
            role="footer",
        )

    def shape(self, tag, **attributes):
        return etree.SubElement(
            self.root,
            f"{{{SVG_NS}}}{tag}",
            {key: str(value) for key, value in attributes.items()},
        )

    def text(
        self,
        field,
        x,
        y,
        width,
        height,
        size,
        *,
        required=True,
        max_chars=240,
        role="stage",
        color=None,
    ):
        node_id = f"slot_{len(self.slots)}"
        self.slots.append(
            Slot(node_id=node_id, field=field, required=required, max_chars=max_chars)
        )
        self.shape(
            "text",
            id=node_id,
            x=x,
            y=y,
            fill=color or self.theme.foreground,
            **{
                "font-size": size,
                "data-box-w": width,
                "data-box-h": height,
                "data-role": role,
            },
        )

    def label(self, value, x, y, size=24):
        node = self.shape(
            "text",
            x=x,
            y=y,
            fill=self.theme.accent,
            **{"font-size": size, "data-role": "stage"},
        )
        node.text = value

    def header(self):
        self.text("title", 80, 110, 1120, 58, 38, max_chars=56, role="title")
        self.text(
            "subtitle",
            80,
            176,
            1120,
            62,
            22,
            required=False,
            max_chars=120,
            color=self.theme.muted,
        )

    def block(self, index, x, heading_y, body_y, width, body_height=208, kind="point"):
        self.text(
            f"blocks[{index}].heading",
            x,
            heading_y,
            width,
            76,
            28,
            required=False,
            max_chars=36,
        )
        self.text(
            f"blocks[{index}].body",
            x,
            body_y,
            width,
            body_height,
            24,
            required=False,
            max_chars=240,
        )
        return ContentBlock(
            id=f"b{index + 1}",
            kind=kind,
            heading=("明确目标", "验证效果", "持续改进")[index % 3],
            body="以实际需求为起点，明确责任与验收标准，结合反馈调整下一步行动。",
        )

    def component(
        self,
        component_id,
        family,
        description,
        example,
        *,
        blocks=0,
        metrics=0,
        images=0,
    ):
        return PageComponent(
            id=component_id,
            family=family,
            description=description,
            svg=serialize(self.root),
            slots=tuple(self.slots),
            blocks=ItemRange(minimum=blocks, maximum=blocks),
            metrics=ItemRange(minimum=metrics, maximum=metrics),
            images=ItemRange(minimum=images, maximum=images),
            examples=(example,),
        )


def load_builtin_package() -> TemplatePackage:
    """Return an immutable snapshot; change the version when modifying published art."""
    theme = PackageTheme()
    components = []

    for family, title, subtitle in (
        (
            "cover",
            "让每一次表达都有清晰的方向",
            "从内容出发，以结构组织观点，用证据支持行动。",
        ),
        ("section", "从想法走向行动", "第二章 · 建立可验证的实践路径"),
    ):
        page = _Page(theme)
        page.text(
            "title",
            80,
            290,
            1080,
            160,
            60 if family == "cover" else 52,
            max_chars=64,
            role="title",
        )
        page.text(
            "subtitle",
            84,
            472,
            1000,
            94,
            28,
            required=False,
            max_chars=120,
            color=theme.muted,
        )
        page.label("LANDPPT / NOTES" if family == "cover" else "CHAPTER", 84, 194, 22)
        components.append(
            page.component(
                family,
                family,
                "适合演示开场" if family == "cover" else "适合章节分隔",
                PageContent(slide_id=family, title=title, subtitle=subtitle),
            )
        )

    page = _Page(theme)
    page.header()
    blocks = []
    for index in range(3):
        x = 80 + index * 386
        page.label(f"0{index + 1}", x, 268)
        blocks.append(page.block(index, x, 326, 414, 348, 164))
    components.append(
        page.component(
            "points_3",
            "points",
            "三个并列观点，各有说明",
            PageContent(
                slide_id="points",
                title="把目标转化为可执行的步骤",
                blocks=tuple(blocks),
            ),
            blocks=3,
        )
    )

    page = _Page(theme)
    page.header()
    page.shape(
        "line",
        x1=640,
        y1=238,
        x2=640,
        y2=574,
        stroke=theme.accent,
        **{"stroke-width": "2"},
    )
    blocks = tuple(
        page.block(index, 80 + index * 620, 286, 386, 500, 188, kind="comparison")
        for index in range(2)
    )
    components.append(
        page.component(
            "comparison_2",
            "comparison",
            "两个方案、状态或立场的对比",
            PageContent(
                slide_id="comparison",
                title="用一致的标准比较不同路径",
                relation="comparison",
                blocks=blocks,
            ),
            blocks=2,
        )
    )

    for count in (3, 4, 5):
        page = _Page(theme)
        page.header()
        width = (1120 - 32 * (count - 1)) / count
        page.shape("line", x1=104, y1=270, x2=1176, y2=270, stroke=theme.muted)
        blocks = []
        for index in range(count):
            x = 80 + index * (width + 32)
            page.shape(
                "circle",
                cx=x + 24,
                cy=270,
                r=24,
                fill=theme.background,
                stroke=theme.accent,
                **{"stroke-width": "2"},
            )
            page.label(str(index + 1), x + 17, 278, 22)
            page.text(
                f"blocks[{index}].heading",
                x,
                346,
                width,
                72,
                26,
                required=False,
                max_chars=24,
            )
            page.text(
                f"blocks[{index}].body",
                x,
                436,
                width,
                154,
                22,
                required=False,
                max_chars=100,
            )
            blocks.append(
                ContentBlock(
                    id=f"b{index + 1}",
                    kind="step",
                    heading=f"阶段{index + 1}",
                    body="明确责任，完成验证，再进入下一阶段。",
                )
            )
        components.append(
            page.component(
                f"process_{count}",
                "process",
                f"{count}个按顺序推进的阶段",
                PageContent(
                    slide_id=f"process_{count}",
                    title="分阶段推进，让结果可以验证",
                    relation="sequence",
                    blocks=tuple(blocks),
                ),
                blocks=count,
            )
        )

    page = _Page(theme)
    page.header()
    block = page.block(0, 80, 286, 394, 500, 182, kind="paragraph")
    page.slots.append(Slot(node_id="visual", field="visual_briefs[0].id", kind="image"))
    page.shape(
        "image",
        id="visual",
        x=656,
        y=236,
        width=544,
        height=340,
        **{"data-role": "stage"},
    )
    components.append(
        page.component(
            "image_1",
            "image",
            "一幅配图与一段解释，图片为必需素材",
            PageContent(
                slide_id="image",
                title="让配图为观点提供具体语境",
                blocks=(block,),
                visual_briefs=(
                    VisualBrief(id="visual1", brief="团队围绕白板讨论实施路径"),
                ),
            ),
            blocks=1,
            images=1,
        )
    )

    page = _Page(theme)
    page.header()
    metrics = []
    for index in range(3):
        x = 80 + index * 386
        page.text(
            f"metrics[{index}].value",
            x,
            346,
            348,
            104,
            64,
            max_chars=12,
            color=theme.accent,
        )
        page.text(
            f"metrics[{index}].unit", x, 430, 348, 42, 26, required=False, max_chars=20
        )
        page.text(f"metrics[{index}].label", x, 508, 348, 74, 26, max_chars=40)
        metrics.append(
            Metric(
                id=f"m{index + 1}",
                label=("示例项目数", "示例验证项", "示例完成率")[index],
                value=("12", "36", "90")[index],
                unit=("项", "项", "%")[index],
                source_refs=("demo",),
            )
        )
    components.append(
        page.component(
            "metrics_3",
            "metrics",
            "三个有来源的关键指标",
            PageContent(
                slide_id="metrics",
                title="用数据回答关键问题",
                subtitle="演示数据，仅用于版式预览",
                relation="evidence",
                metrics=tuple(metrics),
                source_refs=("demo",),
            ),
            metrics=3,
        )
    )

    page = _Page(theme)
    page.header()
    blocks = []
    for index in range(3):
        y = 286 + index * 112
        page.label(f"0{index + 1}", 80, y, 26)
        page.text(
            f"blocks[{index}].heading",
            146,
            y,
            316,
            70,
            28,
            required=False,
            max_chars=32,
        )
        page.text(
            f"blocks[{index}].body", 520, y, 680, 80, 24, required=False, max_chars=140
        )
        blocks.append(
            ContentBlock(
                id=f"b{index + 1}",
                kind="action",
                heading=("确认优先级", "明确下一步", "建立反馈机制")[index],
                body="将讨论结果落实到具体任务，并明确负责人和完成时间。",
            )
        )
    components.append(
        page.component(
            "summary_3",
            "summary",
            "三项结论与行动建议",
            PageContent(
                slide_id="summary",
                title="让结论推动下一步行动",
                relation="summary",
                blocks=tuple(blocks),
            ),
            blocks=3,
        )
    )

    return TemplatePackage(
        package_id="editorial",
        version=1,
        name="清晰叙事",
        description="暖白与墨绿的编辑式版面，覆盖八类页面，流程支持三至五步。",
        theme=theme,
        components=tuple(components),
    )
