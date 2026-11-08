"""Native skill CRUD and composer selection backed by versioned local assets."""

import json
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


class SkillDependenciesUnavailable(Exception):
    def __init__(self, references):
        # Only versioned tool identifiers from validated, visible recipes are exposed.
        self.references = tuple(sorted(references))


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
        # Called after idempotency and scoped model/browser/web grants, inside the send
        # transaction. The Worker never consults a mutable catalog or the Hub.
        entries = self.skills.catalog(scope, connection=connection)
        name = requested_name(text)
        if name is not None:
            selected, _, _ = find_by_name(entries, name)
            missing = set(selected.manifest.required_tools) - set(config.tool_references)
            missing.update(
                set(selected.manifest.required_tools) & set(config.tools) - set(self.tool_allowlist)
            )
            if missing:
                raise SkillDependenciesUnavailable(missing)
            BUILTIN_TOOLS.select(config.tools, self.tool_allowlist)
            bound = (selected,) + tuple(
                s for s in config.skills if s.reference != selected.reference
            )
            config = ExecutionSnapshot.model_validate(
                {**config.model_dump(), "skills": bound, "requested_skill": selected.reference}
            )
        # Discover only executable recipes, with pinned bindings taking precedence over
        # newer catalog versions. Selection never expands an Agent/deployment tool grant.
        refs = set(config.tool_references) - (set(config.tools) - set(self.tool_allowlist))
        seen = {(s.source_id, s.manifest.skill_id) for s in config.skills}
        discovered = []
        omitted = 0
        content_size = sum(len(s.manifest.content) for s in config.skills)
        metadata_size = 0
        budget = min(8000, config.model.context_max_chars // 8)
        if config.model.context_max_bytes is not None:
            budget = min(budget, config.model.context_max_bytes // 8)
        # Personal recipes first, then builtins, then installed Hub recipes; newest per
        # identity. Explicit /selection is resolved above against the complete catalog.
        candidates = sorted(
            entries,
            key=lambda e: (
                {"user": 0, "builtin": 1, "hub": 2}[e[0].source],
                -e[0].manifest.version,
                e[0].reference,
            ),
        )
        for skill, _, _ in candidates:
            identity = (skill.source_id, skill.manifest.skill_id)
            if identity in seen or not set(skill.manifest.required_tools) <= refs:
                continue
            seen.add(identity)
            size = len(json.dumps(skill.catalog_entry(), ensure_ascii=False).encode()) + 2
            if (
                len(discovered) >= 32
                or content_size + len(skill.manifest.content) > 128000
                or metadata_size + size > budget
            ):
                omitted += 1
                continue
            discovered.append(skill)
            metadata_size += size
            content_size += len(skill.manifest.content)
        return ExecutionSnapshot.model_validate(
            {
                **config.model_dump(),
                "catalog_skills": discovered,
                "catalog_omitted": omitted,
            }
        )
