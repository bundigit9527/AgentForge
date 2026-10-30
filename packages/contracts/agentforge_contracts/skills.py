from agentforge_core.skills import Digest, HubCatalog, SkillId, SkillSnapshot, SkillVersion
from pydantic import BaseModel, ConfigDict, Field


class InstallSkill(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(pattern=r"^hub:[a-f0-9]{64}$")
    skill_id: SkillId
    version: SkillVersion
    sha256: Digest


class SkillPage(BaseModel):
    items: list[SkillSnapshot]
    next_offset: int | None


class RemoteSkillPage(HubCatalog):
    source_id: str
