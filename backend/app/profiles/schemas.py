from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Version = Annotated[int, Field(strict=True, ge=0)]
Count = Annotated[int, Field(strict=True, ge=1, le=10)]
ContentType = Literal["article", "repository", "release", "conference_index"]


def unique_values(values):
    seen, result = set(), []
    for value in values:
        key = value.casefold() if isinstance(value, str) else value
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


class ProfileData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    background: str = Field(default="", max_length=2000)
    foundations: list[Tag] = Field(default_factory=list, max_length=20)
    long_term_directions: list[Tag] = Field(default_factory=list, max_length=20)
    learning_goals: list[Tag] = Field(default_factory=list, max_length=20)
    language_preferences: list[Literal["zh", "en"]] = Field(default_factory=list, max_length=2)
    daily_reading_minutes: int | None = Field(default=None, strict=True, ge=1, le=240)
    blocked_topics: list[Tag] = Field(default_factory=list, max_length=20)

    @field_validator("background")
    @classmethod
    def strip_background(cls, value):
        return value.strip()

    @field_validator(
        "foundations",
        "long_term_directions",
        "learning_goals",
        "language_preferences",
        "blocked_topics",
    )
    @classmethod
    def deduplicate(cls, values):
        return unique_values(values)


class ProfilePatch(ProfileData):
    expected_version: Version


class FieldMetadata(BaseModel):
    source: Literal["user_explicit"]
    updated_at: datetime


class ProfileRead(BaseModel):
    version: int
    data: ProfileData
    field_metadata: dict[str, FieldMetadata]
    updated_at: datetime | None


class RevisionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    version: int
    data: ProfileData
    field_metadata: dict[str, FieldMetadata]
    changed_fields: list[str]
    created_at: datetime


class ColumnData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name
    keywords: list[Tag] = Field(default_factory=list, max_length=20)
    source_ids: list[UUID] = Field(default_factory=list, max_length=50)
    content_types: list[ContentType] = Field(default_factory=list, max_length=4)
    items_per_day: Count = 2
    enabled: bool = Field(default=True, strict=True)

    @field_validator("keywords", "source_ids", "content_types")
    @classmethod
    def deduplicate(cls, values):
        return unique_values(values)

    @model_validator(mode="after")
    def require_condition(self):
        if not (self.keywords or self.source_ids or self.content_types):
            raise ValueError("至少填写关键词、选择来源或选择内容类型，不能创建无条件栏目")
        return self


class ColumnPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: Version
    name: Name | None = None
    keywords: list[Tag] | None = Field(default=None, max_length=20)
    source_ids: list[UUID] | None = Field(default=None, max_length=50)
    content_types: list[ContentType] | None = Field(default=None, max_length=4)
    items_per_day: Count | None = None
    enabled: bool | None = Field(default=None, strict=True)

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null(cls, values):
        if isinstance(values, dict) and any(value is None for value in values.values()):
            raise ValueError("栏目字段不能为 null；清空筛选条件请使用空数组")
        return values


class ColumnRead(ColumnData):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    version: int
    created_at: datetime
    updated_at: datetime
