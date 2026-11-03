"""Native skill CRUD and composer selection backed by versioned local assets."""

import re

from agentforge_contracts.rakazo import SkillTarget
from agentforge_core.execution import ExecutionSnapshot
from agentforge_core.identity import PermissionDenied
from agentforge_core.models import RunConflict, RunNotFound
from agentforge_runtime.skill_tools import skill_document
from agentforge_runtime.tools import BUILTIN_TOOLS
from agentforge_storage.thread_feed import iso


def catalog_name(skill):
    if skill.source == "user":
        return skill.manifest.name
    # Composer sends a name, not an ID. Qualify installed versions so another version or
    # installation never silently takes over a saved /name. Detect short-ID collisions below.
    suffix = f" [v{skill.manifest.version}:{skill.reference[:8]}]" if skill.source == "hub" else ""
    name = " ".join(skill.manifest.name.split())
    # Match the original contract's 80 UTF-16 code-unit name limit.
    name = name.encode("utf-16-le")[: (80 - len(suffix)) * 2].decode("utf-16-le", errors="ignore")
    return name.rstrip() + suffix


def catalog_entry(skill):
    return {
        "id": skill.source_id[5:] if skill.source == "user" else skill.reference,
        "name": catalog_name(skill),
        "description": skill.manifest.description,
        "source": "plugin" if skill.source == "hub" else skill.source,
        "readOnly": skill.source != "user",
    }


def find_by_name(entries, name):
    found = [entry for entry in entries if catalog_name(entry[0]).casefold() == name.casefold()]
    if not found:
        raise RunNotFound()
    if len(found) != 1:
        raise RunConflict("Ambiguous skill name")
    return found[0]


def requested_name(text):
    match = re.match(r"^(?:/|use\s+skill:\s*)(.+?)(?:\n|$)", text.lstrip(), re.IGNORECASE)
    if match is None:
        return None
    name = match[1].strip().lstrip("/").strip()
    if (
        not name
        or len(name.encode("utf-16-le")) // 2 > 80
        or re.match(r"^(chat\s+settings|settings:)", name, re.IGNORECASE)
    ):
        return None
    return name


class SkillRpcAdapter:
    def __init__(self, skills, tool_allowlist):
        self.skills = skills
        self.tool_allowlist = tool_allowlist

    def list(self, scope):
        return [catalog_entry(skill) for skill, _, _ in self.skills.catalog(scope)]

    def get(self, scope, target):
        entries = self.skills.catalog(scope)
        if target.skillId:
            found = next(
                (entry for entry in entries if catalog_entry(entry[0])["id"] == target.skillId),
                None,
            )
            if found is None:
                raise RunNotFound()
        else:
            found = find_by_name(entries, target.name)
        return self.detail(found)

    @staticmethod
    def detail(found):
        skill, created, updated = found
        return {
            **catalog_entry(skill),
            "content": skill_document(skill, name=catalog_name(skill)),
            "createdAt": iso(created),
            "updatedAt": iso(updated),
        }

    def create(self, scope, body):
        return self.detail(self.skills.create_user(scope, body.model_dump(exclude_unset=True)))

    def update(self, scope, body):
        if self.get(scope, SkillTarget(skillId=body.skillId))["readOnly"]:
            raise PermissionDenied()
        return self.detail(
            self.skills.update_user(
                scope,
                body.skillId,
                body.model_dump(exclude_unset=True, exclude={"skillId", "expectedVersion"}),
                body.expectedVersion,
            )
        )

    def remove(self, scope, body):
        if self.get(scope, SkillTarget(skillId=body.skillId))["readOnly"]:
            raise PermissionDenied()
        self.skills.user.remove(scope, body.skillId)
        return {"ok": True}

    def prepare_run(self, scope, text, config, *, connection):
        name = requested_name(text)
        if name is None:
            return config
        selected, _, _ = find_by_name(self.skills.catalog(scope, connection=connection), name)
        # Selection can narrow a task's guidance, never expand the assistant's tool grants.
        BUILTIN_TOOLS.select(config.tools, self.tool_allowlist)
        bound = (selected,) + tuple(s for s in config.skills if s.reference != selected.reference)
        return ExecutionSnapshot.model_validate(
            {**config.model_dump(), "skills": bound, "requested_skill": selected.reference}
        )
