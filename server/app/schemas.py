"""Request/response models. Input validation lives here, at the API boundary."""

import math
from typing import Annotated, Self
from urllib.parse import urlparse

from pydantic import AfterValidator, BaseModel, Field, model_validator

from .jobs import JobStatus
from .limits import max_clip_seconds
from .media import Mode

_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}


def _require_youtube(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in _YOUTUBE_HOSTS:
        raise ValueError("Only YouTube links are supported")
    return url


YouTubeUrl = Annotated[str, AfterValidator(_require_youtube)]


class InfoRequest(BaseModel):
    url: YouTubeUrl


class TranscriptRequest(InfoRequest):
    fallback_whisper: bool = False


class ClipRequest(InfoRequest):
    start: float = Field(ge=0)
    end: float
    res: int = Field(gt=0)
    mode: Mode = Mode.EXACT

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if not math.isfinite(self.start) or not math.isfinite(self.end):
            raise ValueError("Range values must be finite")
        limit = max_clip_seconds(self.res)
        if not 0 < self.end - self.start <= limit:
            raise ValueError(
                f"At {self.res}p a clip can be up to {limit // 60} minutes. "
                "Pick a shorter part or a lower quality."
            )
        return self


class RangeRequest(BaseModel):
    start: float = Field(ge=0)
    end: float

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if not math.isfinite(self.start) or not math.isfinite(self.end):
            raise ValueError("Range values must be finite")
        if self.end <= self.start:
            raise ValueError("Each range must have a positive duration")
        return self


class ExportRequest(InfoRequest):
    ranges: list[RangeRequest] = Field(min_length=1, max_length=20)
    res: int = Field(gt=0)
    mode: Mode = Mode.EXACT

    @model_validator(mode="after")
    def _check_ranges(self) -> Self:
        limit = max_clip_seconds(self.res)
        if any(item.end - item.start > limit for item in self.ranges):
            raise ValueError(
                f"At {self.res}p a clip can be up to {limit // 60} minutes. "
                "Pick shorter ranges or a lower quality."
            )
        merged: list[RangeRequest] = []
        for item in sorted(self.ranges, key=lambda value: value.start):
            if merged and item.start <= merged[-1].end:
                merged[-1] = RangeRequest(
                    start=merged[-1].start,
                    end=max(merged[-1].end, item.end),
                )
            else:
                merged.append(item)
        if any(item.end - item.start > limit for item in merged):
            raise ValueError(
                f"At {self.res}p a merged range can be up to {limit // 60} minutes. "
                "Pick shorter ranges or a lower quality."
            )
        if sum(item.end - item.start for item in merged) > 3 * 60 * 60:
            raise ValueError("The combined export duration cannot exceed 180 minutes")
        self.ranges = merged
        return self


class QualityOut(BaseModel):
    res: int
    label: str
    kbps: int
    max_seconds: int
    fps: float | None = None
    codec: str | None = None
    container: str | None = None
    format_id: str | None = None
    has_audio: bool
    audio_available: bool


class InfoResponse(BaseModel):
    id: str
    title: str
    duration: float
    thumbnail: str | None
    qualities: list[QualityOut]


class TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str
    words: list[str] | None = None


class TranscriptResponse(BaseModel):
    available: bool
    reason: str | None = None
    segments: list[TranscriptSegment] = Field(default_factory=list)


class TranscriptJobOut(BaseModel):
    id: str
    status: JobStatus
    position: int | None = None
    phase: str = "queued"
    percent: int = Field(default=0, ge=0, le=100)
    error: str | None = None
    segments: list[TranscriptSegment] = Field(default_factory=list)


class JobOut(BaseModel):
    id: str
    status: JobStatus
    position: int | None = None  # 1 = next in line, only while queued
    elapsed_seconds: float | None = None
    estimate_seconds: float | None = None
    error: str | None = None
    phase: str = "queued"
    percent: int = Field(default=0, ge=0, le=100)
    filename: str | None = None  # the fields below are set once the clip is done
    size_bytes: int | None = None
    expires_in: int | None = None


class ExportJobOut(JobOut):
    filename: str | None = None


class SourcePresignRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str


class SourcePresignOut(BaseModel):
    id: str
    status: str
    upload_url: str | None = None
    upload_fields: dict[str, str] | None = None
    expires_in: int


class SourceOut(BaseModel):
    id: str
    source_type: str
    title: str
    duration_seconds: float
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    codec: str | None = None
    status: str
    expires_in: int


class SourceCompleteOut(SourceOut):
    pass
