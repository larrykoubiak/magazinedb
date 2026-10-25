from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ExtractedIndexEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(
        min_length=1,
        description="Only the exact name of one video game; never a sentence or article blurb",
    )
    printed_page: int | None
    uncertain: bool


class ExtractedIndexSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: Literal["reviews", "previews", "news", "features", "guides", "contents", "other"]
    entries: list[ExtractedIndexEntry] = Field(default_factory=list)


class IndexPageExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sections: list[ExtractedIndexSection] = Field(default_factory=list)
