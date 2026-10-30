from agentforge_core.models import RunConflict, RunNotFound
from agentforge_core.skills import SkillHub
from agentforge_storage.skills import SkillRepository

from agentforge_skills.builtin import BUILTIN_SKILLS


class SkillService:
    def __init__(self, repository: SkillRepository, hub: SkillHub):
        self.repository = repository
        self.hub = hub
        self.builtin = {skill.reference: skill for skill in BUILTIN_SKILLS}

    def list(self, scope, offset, limit):
        local = list(self.builtin.values())[offset : offset + limit]
        return local + self.repository.list(
            scope, max(0, offset - len(self.builtin)), limit - len(local)
        )

    def resolve(self, scope, references):
        resolved = []
        for reference in references:
            if reference.startswith("builtin:"):
                skill = self.builtin.get(reference)
                if skill is None:
                    raise RunNotFound()
            else:
                skill = self.repository.get(scope, reference)
            resolved.append(skill)
        return tuple(resolved)

    def install(self, scope, request):
        if request.source_id != self.hub.source_id:
            raise RunConflict("Skill Hub source has changed; reload the catalog")
        manifest = self.hub.manifest(request.skill_id, request.version)
        if manifest.digest() != request.sha256:
            raise RunConflict("Skill manifest has changed; reload the catalog")
        return self.repository.install(scope, self.hub.source_id, manifest, request.sha256)
