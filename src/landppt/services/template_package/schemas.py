"""First package contract: immutable, JSON-serializable page drafts and SVG slots."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$")]
ShortText = Annotated[str, Field(max_length=2000)]
SlotField = Annotated[
    str,
    Field(
        max_length=100,
        pattern=r"^(title|subtitle|takeaway|blocks\[[0-9]+\]\.(heading|body)|"
        r"metrics\[[0-9]+\]\.(label|value|unit)|visual_briefs\[[0-9]+\]\.id)$",
    ),
]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ContentBlock(ContractModel):
    id: Identifier
    kind: Literal["paragraph", "point", "step", "comparison", "action"] = "point"
    heading: ShortText = ""
    body: ShortText = ""
    source_refs: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def require_text(self):
        if not self.heading and not self.body:
            raise ValueError("a content block must contain text")
        return self


class Metric(ContractModel):
    id: Identifier
    label: Annotated[str, Field(min_length=1, max_length=200)]
    value: Annotated[str, Field(min_length=1, max_length=100)]
    unit: Annotated[str, Field(max_length=100)] = ""
    source_refs: Annotated[tuple[Identifier, ...], Field(min_length=1)]


class VisualBrief(ContractModel):
    id: Identifier
    brief: Annotated[str, Field(min_length=1, max_length=2000)]


class PageContent(ContractModel):
    """Final copy, not an outline; unsupported fields are rejected, never discarded."""

    slide_id: Identifier
    content_revision: Annotated[int, Field(strict=True, ge=1)] = 1
    title: Annotated[str, Field(min_length=1, max_length=500)]
    subtitle: ShortText = ""
    intent: ShortText = ""
    relation: Literal["overview", "sequence", "comparison", "evidence", "summary"] = (
        "overview"
    )
    blocks: Annotated[tuple[ContentBlock, ...], Field(max_length=20)] = ()
    metrics: Annotated[tuple[Metric, ...], Field(max_length=12)] = ()
    takeaway: ShortText = ""
    source_refs: tuple[Identifier, ...] = ()
    visual_briefs: Annotated[tuple[VisualBrief, ...], Field(max_length=8)] = ()

    @model_validator(mode="after")
    def validate_references(self):
        for collection in (self.blocks, self.metrics, self.visual_briefs):
            ids = [item.id for item in collection]
            if len(ids) != len(set(ids)):
                raise ValueError("content IDs must be unique within each collection")
        known = set(self.source_refs)
        for item in (*self.blocks, *self.metrics):
            if not set(item.source_refs).issubset(known):
                raise ValueError("content references must be declared in source_refs")
        return self


class Slot(ContractModel):
    node_id: Identifier
    field: SlotField
    kind: Literal["text", "image"] = "text"
    required: bool = True
    max_chars: Annotated[int, Field(strict=True, ge=1, le=2000)] = 500

    @model_validator(mode="after")
    def validate_binding_kind(self):
        if self.field.startswith("visual_briefs[") != (self.kind == "image"):
            raise ValueError("image slots must bind a visual_briefs ID")
        if "[0" in self.field and "[0]" not in self.field:
            raise ValueError("slot indices must use canonical decimal notation")
        return self


class ItemRange(ContractModel):
    minimum: Annotated[int, Field(strict=True, ge=0, le=20)] = 0
    maximum: Annotated[int, Field(strict=True, ge=0, le=20)] = 0

    @model_validator(mode="after")
    def validate_range(self):
        if self.minimum > self.maximum:
            raise ValueError("minimum must not exceed maximum")
        return self


class PageComponent(ContractModel):
    id: Identifier
    family: Literal[
        "cover",
        "section",
        "points",
        "comparison",
        "process",
        "image",
        "metrics",
        "summary",
    ]
    description: Annotated[str, Field(min_length=1, max_length=2000)]
    svg: Annotated[str, Field(min_length=1, max_length=200000)]
    slots: Annotated[tuple[Slot, ...], Field(min_length=1, max_length=100)]
    blocks: ItemRange = ItemRange()
    metrics: ItemRange = ItemRange()
    images: ItemRange = ItemRange()
    examples: Annotated[tuple[PageContent, ...], Field(min_length=1, max_length=10)]

    @model_validator(mode="after")
    def validate_slots(self):
        ids = [slot.node_id for slot in self.slots]
        fields = [slot.field for slot in self.slots]
        if len(set(ids)) != len(ids) or len(set(fields)) != len(fields):
            raise ValueError("each node and content field may be bound only once")
        if not any(slot.field == "title" and slot.required for slot in self.slots):
            raise ValueError("a required title slot is mandatory")
        return self


class PackageTheme(ContractModel):
    background: Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")] = "#F4F1EA"
    foreground: Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")] = "#182F36"
    accent: Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")] = "#B7462D"
    muted: Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")] = "#50646A"


class TemplatePackage(ContractModel):
    contract_version: Literal[1] = 1
    package_id: Identifier
    version: Annotated[int, Field(strict=True, ge=1)]
    name: Annotated[str, Field(min_length=1, max_length=255)]
    description: ShortText = ""
    language: Literal["zh-CN", "en"] = "zh-CN"
    template_kind: Literal["package"] = "package"
    render_mode: Literal["svg"] = "svg"
    canvas_width: Literal[1280] = 1280
    canvas_height: Literal[720] = 720
    theme: PackageTheme = PackageTheme()
    components: Annotated[tuple[PageComponent, ...], Field(min_length=1, max_length=40)]

    @model_validator(mode="after")
    def validate_component_ids(self):
        ids = [component.id for component in self.components]
        if len(ids) != len(set(ids)):
            raise ValueError("component IDs must be unique")
        return self

    def content_hash(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
