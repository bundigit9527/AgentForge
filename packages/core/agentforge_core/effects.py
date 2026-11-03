"""Immutable, bounded external operation requests; not tool authorization."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def bounded_json(value):
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (ValueError, TypeError, RecursionError):
        raise ValueError("Invalid operation data") from None
    if len(encoded) > 20000:
        raise ValueError("Operation data exceeds limit")
    return encoded


class EffectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tool_reference: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}@[1-9][0-9]{0,8}$")
    target: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    arguments: dict

    @model_validator(mode="after")
    def bounded(self):
        bounded_json(self.model_dump())
        return self

    def digest(self):
        return hashlib.sha256(bounded_json(self.model_dump()).encode()).hexdigest()


class EffectDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
