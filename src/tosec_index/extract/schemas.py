from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ExtractedMention(BaseModel):
    model_config = ConfigDict(extra="forbid")

    printed_name: str = Field(min_length=1)
    entity_type: Literal[
        "software", "platform", "article", "person", "developer", "publisher", "subject"
    ]
    page_number: int = Field(ge=1)
    supporting_excerpt: str = Field(min_length=1)
    context: Literal[
        "primary_subject", "passing_reference", "comparison", "credit", "advertisement", "unknown"
    ]
    relationship: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    uncertainty: str | None = None
    content_type: Literal[
        "review",
        "preview",
        "interview",
        "advertisement",
        "guide",
        "news",
        "editorial",
        "other",
        "unknown",
    ] = "unknown"


class PageExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mentions: list[ExtractedMention] = Field(default_factory=list)
