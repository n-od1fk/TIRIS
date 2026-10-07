from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.profiles.schemas import Tag, Version, unique_values


class DiscoveryData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recent_topics: list[Tag] = Field(default_factory=list, max_length=10)
    daily_enabled: bool = Field(default=False, strict=True)
    hour: int = Field(default=9, strict=True, ge=0, le=23)
    github_search: bool = Field(default=False, strict=True)
    matching: Literal["rules", "local"] = "rules"
    semantic_threshold: float = Field(default=0.55, ge=0, le=1)

    @field_validator("recent_topics")
    @classmethod
    def distinct(cls, value):
        return unique_values(value)


class DiscoveryPatch(DiscoveryData):
    expected_version: Version
