from typing import Annotated, Literal
from uuid import UUID

from agentforge_core.attachments import FILE_TYPES, filename
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


class RpcInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Empty(RpcInput):
    pass


class SetDefaultModel(RpcInput):
    provider: str = Field(min_length=1, max_length=80)
    modelId: str = Field(min_length=1, max_length=128)
    thinkingLevel: None = None


class Target(RpcInput):
    botId: UUID


class DesktopFile(Target):
    model_config = ConfigDict(str_strip_whitespace=False)
    path: str = Field(min_length=1, max_length=1024)


class ListDesktopFiles(DesktopFile):
    path: str = Field(default="/", max_length=1024)


class UploadDesktopFile(DesktopFile):
    contentBase64: str = Field(max_length=13_981_016)


class SetComputer(Target):
    mode: Literal["team", "dedicated"]


class Bootstrap(RpcInput):
    botId: UUID | None = None


class History(Target):
    before: int | None = Field(default=None, ge=0, le=2**53 - 1, strict=True)
    includePeerRuns: Literal[False] = False
    includePeerReceipts: Literal[False] = False


class Subscribe(Target):
    cursor: int = Field(ge=-1, le=2**53 - 1, strict=True)


class Send(Target):
    text: str = Field(default="", max_length=10000)
    clientNonce: str | None = Field(default=None, min_length=1, max_length=200)
    artifactIds: list[UUID] = Field(default_factory=list, max_length=4)
    mentions: list = Field(default_factory=list, max_length=0)

    @model_validator(mode="after")
    def has_message(self):
        if not self.text and not self.artifactIds:
            raise ValueError("Provide text or attachments")
        return self


class CreateArtifact(Target):
    name: Annotated[str, Field(min_length=1, max_length=255), AfterValidator(filename)]
    description: str | None = Field(default=None, max_length=280)
    mimeType: str
    contentBase64: str = Field(min_length=1, max_length=13_981_016)
    clientNonce: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def known_type(self):
        if self.mimeType not in FILE_TYPES:
            raise ValueError("Unsupported attachment type")
        return self


class CreateBot(RpcInput):
    name: str = Field(min_length=1, max_length=80)
    title: str = Field(default="", max_length=500)
    description: str = Field(default="", max_length=4000)
    instructions: str = Field(default="", max_length=10000)
    notifyOnFinish: bool = Field(default=False, strict=True)
    color: str | None = Field(default=None, max_length=200)
    computerMode: Literal["team", "dedicated"] = "team"
    spawnKey: str | None = Field(default=None, min_length=1, max_length=120)


class UpdateBot(Target):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    title: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=4000)
    instructions: str | None = Field(default=None, max_length=10000)
    notifyOnFinish: bool | None = Field(default=None, strict=True)
    color: str | None = Field(default=None, max_length=200)
    pinned: bool | None = Field(default=None, strict=True)
    # Disabled defaults are sent by the original settings form even on unrelated saves.
    memoryScope: None = None
    voiceId: None = None
    thinkingLevel: None = None
    sectionId: None = None
    autoSpeak: bool = Field(default=False, strict=True)
    teamChatAmbientEnabled: bool = Field(default=False, strict=True)
    teamChatRules: Literal[""] = ""
    disabledBuiltinTools: list[str] = Field(default_factory=list, max_length=0)
    modelProvider: str | None = Field(default=None, max_length=80)
    modelId: str | None = Field(default=None, max_length=200)
    expectedVersion: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def supported_settings(self):
        if self.autoSpeak or self.teamChatAmbientEnabled:
            raise ValueError("This capability is not implemented")
        supplied = self.model_fields_set
        if ("modelProvider" in supplied) != ("modelId" in supplied):
            raise ValueError("Model provider and model ID must be supplied together")
        for field in (
            "name",
            "title",
            "description",
            "instructions",
            "notifyOnFinish",
            "color",
            "pinned",
            "expectedVersion",
        ):
            if field in supplied and getattr(self, field) is None:
                raise ValueError("This setting cannot be null")
        return self


class ConversationList(Target):
    offset: int = Field(default=0, ge=0, le=100000, strict=True)
    limit: int = Field(default=50, ge=1, le=100, strict=True)


class CreateConversation(Target):
    expectedThreadId: UUID
    requestId: UUID
    title: str = Field(default="", max_length=200)


class SelectConversation(Target):
    expectedThreadId: UUID
    threadId: UUID


class SkillTarget(RpcInput):
    skillId: str | None = Field(default=None, min_length=1, max_length=160)
    name: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def identified(self):
        if not (self.skillId or self.name) or any(
            getattr(self, field) is None for field in self.model_fields_set
        ):
            raise ValueError("Provide skillId or name")
        return self


class CreateSkill(RpcInput):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    content: str | None = Field(default=None, min_length=1, max_length=12000)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, min_length=1, max_length=1000)
    body: str | None = Field(default=None, max_length=12000)

    @model_validator(mode="after")
    def supplied_fields(self):
        if any(getattr(self, f) is None for f in self.model_fields_set):
            raise ValueError("Skill fields cannot be null")
        if isinstance(self, UpdateSkill):
            if not (self.model_fields_set & {"content", "name", "description", "body"}):
                raise ValueError("No skill fields to update")
        elif not (self.content and self.content.strip()) and not (self.name and self.description):
            raise ValueError("Provide SKILL.md or name and description")
        return self


class UpdateSkill(CreateSkill):
    skillId: str = Field(min_length=1, max_length=160)
    expectedVersion: int | None = Field(default=None, ge=1, strict=True)


class RemoveSkill(RpcInput):
    skillId: str = Field(min_length=1, max_length=160)


class Login(RpcInput):
    token: str = Field(min_length=40, max_length=256, repr=False)


class AnswerApproval(Target):
    threadId: UUID | None = None
    runId: UUID
    messageId: UUID
    answer: Literal["allow", "always", "deny"]
    clientNonce: str | None = Field(default=None, min_length=1, max_length=200)
    username: None = None


class ListSpaceArtifacts(RpcInput):
    botId: UUID | None = None
    cursor: UUID | None = None
    limit: int = Field(default=30, ge=1, le=60, strict=True)


class ArtifactId(RpcInput):
    artifactId: UUID


class BotArtifact(Target):
    artifactId: UUID


class ArtifactFamily(RpcInput):
    familyId: UUID


class ReleaseDesktop(Target):
    reason: Literal["done", "skipped"] | None = None
