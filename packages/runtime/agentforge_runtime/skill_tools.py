"""Rakazo-style catalog + skill_read over the current run's immutable recipes."""

import json

from agentforge_core.skills import SkillReference, SkillSnapshot
from pydantic import BaseModel, ConfigDict, field_validator

from agentforge_runtime.tools import ToolDefinition, ToolRegistry

SKILL_READ = "skill_read@1"


def skill_document(skill: SkillSnapshot) -> str:
    manifest = skill.manifest
    # JSON strings are valid YAML scalars; no untrusted YAML is executed or imported.
    name = json.dumps(manifest.name, ensure_ascii=False)
    description = json.dumps(manifest.description, ensure_ascii=False)
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{manifest.content}"


def with_skill_reader(registry: ToolRegistry, skills: tuple[SkillSnapshot, ...]) -> ToolRegistry:
    selected = {skill.reference: skill for skill in skills}

    class ReadArguments(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
        reference: SkillReference

        @field_validator("reference")
        @classmethod
        def selected_only(cls, value):
            if value not in selected:
                raise ValueError("Skill is not available to this run")
            return value

    class ReadResult(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
        reference: str
        name: str
        version: int
        content: str

    def read(arguments):
        skill = selected[arguments.reference]
        return {
            "reference": skill.reference,
            "name": skill.manifest.name,
            "version": skill.manifest.version,
            "content": skill_document(skill),
        }

    return registry.extended(
        [
            ToolDefinition(
                "skill_read",
                1,
                "Read the SKILL.md recipe for a reference from the available skills catalog. "
                "Only reads this run's selected snapshots; cannot install or grant tools.",
                ReadArguments,
                ReadResult,
                read,
            ),
        ]
    )
