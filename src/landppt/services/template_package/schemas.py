"""First package contract: immutable, JSON-serializable page drafts and SVG slots."""

from __future__ import annotations

import base64
import hashlib
import json
from io import BytesIO
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
    reference_asset: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    source_slide: Annotated[int, Field(ge=1, le=100)] | None = None
    sample_assets: dict[
        Identifier, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    ] = Field(default_factory=dict)

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


class PackageAsset(ContractModel):
    """Immutable raster stored with the version, never in the user's image library."""

    id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    media_type: Literal["image/png", "image/jpeg"] = "image/png"
    data: Annotated[str, Field(max_length=12_000_000)]

    @model_validator(mode="after")
    def validate_image(self):
        from PIL import Image

        try:
            raw = base64.b64decode(self.data, validate=True)
            if hashlib.sha256(raw).hexdigest() != self.id:
                raise ValueError("asset hash mismatch")
            with Image.open(BytesIO(raw)) as image:
                expected = "PNG" if self.media_type == "image/png" else "JPEG"
                if image.format != expected or image.width * image.height > 16_000_000:
                    raise ValueError("unsupported asset format or dimensions")
                image.verify()
        except Exception as exc:
            raise ValueError(f"invalid package asset: {exc}") from exc
        return self

    def url(self):
        return f"data:{self.media_type};base64,{self.data}"


class TemplatePackage(ContractModel):
    contract_version: Literal[1, 2] = 1
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
    assets: Annotated[tuple[PackageAsset, ...], Field(max_length=120)] = ()

    @model_validator(mode="after")
    def validate_component_ids(self):
        ids = [component.id for component in self.components]
        if len(ids) != len(set(ids)):
            raise ValueError("component IDs must be unique")
        asset_ids = [asset.id for asset in self.assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("asset IDs must be unique")
        if self.assets and self.contract_version != 2:
            raise ValueError("package assets require contract_version 2")
        for component in self.components:
            references = set(component.sample_assets.values())
            if component.reference_asset:
                references.add(component.reference_asset)
            if not references.issubset(asset_ids):
                raise ValueError("component references missing package assets")
        if sum(len(asset.data) for asset in self.assets) > 80_000_000:
            raise ValueError("package assets exceed 60 MB")
        return self

    def content_hash(self) -> str:
        payload = self.model_dump(mode="json")
        if self.contract_version == 1:
            payload.pop("assets", None)  # Preserve hashes of published v1 packages.
            for component in payload["components"]:
                for field in ("reference_asset", "source_slide", "sample_assets"):
                    if not component[field]:
                        component.pop(field)
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
