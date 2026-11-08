"""Credential-free model management inputs shared by the original Web adapter."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelManagementError(Exception):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


class Choice(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    provider: str = Field(min_length=1, max_length=128)
    modelId: str = Field(min_length=1, max_length=128)


class ConnectModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    provider: Literal["deepseek", "openai-compatible"]
    apiKey: str | None = Field(default=None, max_length=4096, repr=False)
    baseUrl: str | None = Field(default=None, max_length=2048)
    modelId: str = Field(min_length=1, max_length=128)
    label: str | None = Field(default=None, max_length=128)
    reasoning: Literal[False] = False
    thinkingLevel: Literal["off"] | None = None
    maxTokens: int | None = Field(default=None, ge=1, le=131072, strict=True)
    contextWindow: int | None = Field(default=None, ge=1025, le=2147483647, strict=True)
    supportsImages: Literal[False] = False
    maxImagesPerPrompt: int | None = Field(default=None, ge=1, le=1000, strict=True)

    @model_validator(mode="after")
    def valid(self):
        if self.apiKey is not None and (
            not self.apiKey
            or not self.apiKey.isascii()
            or any(ord(c) < 33 or ord(c) == 127 for c in self.apiKey)
        ):
            raise ValueError("Provide a nonempty API key without whitespace")
        if self.provider == "openai-compatible" and not self.baseUrl:
            raise ValueError("Base URL is required")
        if any(ord(c) < 32 for c in self.modelId):
            raise ValueError("Invalid model id")
        if self.contextWindow and self.maxTokens and self.contextWindow - self.maxTokens < 1024:
            raise ValueError("Reserve at least 1024 input tokens")
        return self


class DisconnectModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["deepseek", "openai-compatible"]


class ProbeModels(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    baseUrl: str = Field(min_length=1, max_length=2048)
    apiKey: str | None = Field(default=None, max_length=4096, repr=False)


class ProbeCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    provider: Literal["deepseek"]
    apiKey: str = Field(min_length=8, max_length=4096, repr=False)
