from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field, HttpUrl, model_validator


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


UtcTimestamp = Annotated[datetime, AfterValidator(as_utc)]


class UploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: Literal["image/png", "image/jpeg", "image/webp"]
    size: int | None = Field(default=None, ge=1, le=20 * 1024 * 1024)


class UploadResponse(BaseModel):
    upload_id: str
    upload_url: str
    expires_in: int | None
    upload_headers: dict[str, str] = Field(default_factory=dict)


class Operation(BaseModel):
    type: Literal["resize", "thumbnail", "convert", "metadata"]
    width: int | None = Field(default=None, gt=0, le=10000)
    height: int | None = Field(default=None, gt=0, le=10000)
    format: str | None = None

    @model_validator(mode="after")
    def validate_operation(self):
        if self.type in ("resize", "thumbnail") and (self.width is None or self.height is None):
            raise ValueError("resize and thumbnail require width and height")
        if self.width and self.height and self.width * self.height > 16_000_000:
            raise ValueError("Output exceeds 16 million pixels")
        if self.format is not None and self.format.lower() not in ("png", "webp", "jpeg"):
            raise ValueError("format must be png, webp, or jpeg")
        if self.type == "convert" and not self.format:
            raise ValueError("convert requires a format")
        return self


class JobRequest(BaseModel):
    upload_id: str
    operations: list[Operation] = Field(min_length=1, max_length=16)
    webhook_url: HttpUrl | None = None


class JobResponse(BaseModel):
    id: str
    upload_id: str
    status: str
    progress: int
    operations: list[dict[str, Any]]
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    attempt_count: int
    next_retry_at: UtcTimestamp | None = None
    dead_lettered_at: UtcTimestamp | None = None


class AttemptResponse(BaseModel):
    number: int
    status: str
    started_at: UtcTimestamp
    ended_at: UtcTimestamp | None
    error_message: str | None


class EndpointRequest(BaseModel):
    url: HttpUrl

