from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PackageOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selector: Literal["auto", "jev", "llm", "rules"] = "auto"
    batch_size: int = Field(3, ge=1, le=5)
    image_budget: int = Field(0, ge=0, le=40)
    allow_images: bool = False
    allow_freeform_fallback: bool = True
    repair_attempts: int = Field(1, ge=0, le=2)


PROMPT_VERSION = "package-content-v2-slots"
