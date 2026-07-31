"""Request and response shapes for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models import BrandKit, ChannelSpec


class SubmitBriefRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=4000)
    tenant: str = "acme-pharma"
    compliance_profile: str = "pharma-dtc-us"
    submitted_by: str = "reviewer@example.com"
    brand_kit: BrandKit
    channel: ChannelSpec = Field(default_factory=ChannelSpec)


class SubmitBriefResponse(BaseModel):
    session_id: str
    campaign_id: str
    stream_url: str
    mode: str
    message: str


class SignoffRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=200)
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=2000)


class ReplayRequest(BaseModel):
    speed: float = Field(default=3.0, gt=0.0, le=20.0)


class ReplayResponse(BaseModel):
    session_id: str
    stream_url: str
    recording: dict[str, Any]
    is_replay: Literal[True] = True
